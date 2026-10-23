"""Scheduler rolling forecast (Fase 4) — pengisi `latest`/`history` latar belakang.

Kontrak §5.1: `POST /forecast` **tidak menyimpan** hasil; "penyimpanan terjadi
di scheduler latar belakang". Modul itulah scheduler itu — sebelumnya hanya
disebut di kontrak, belum pernah diimplementasikan (dampak: `GET /forecast/latest`
selalu 404 `E_NO_FORECAST`).

Desain:

- **Rantai identik dengan endpoint** (kontrak §8): scheduler memanggil
  ``service.run_forecast`` yang sama — bukan salinan logika, bukan HTTP loopback.
- **Tanpa FastAPI/Pydantic di sini**: input dari ``app.actuals`` (deret nyata
  ``data/actuals.csv``), output disimpan lewat ``ForecastStore``. Satu-satunya
  Pydantic yang tersentuh adalah ``ForecastPoint`` saat membangun objek simpan —
  konversi wajib karena ``StoredForecast`` menuntut tipe skema (§3.1).
- **History dari data aktual, bukan karangan**: ``load_actual_series`` adalah
  satu-satunya pintu baca CSV (lihat ``app/actuals.py``). Scheduler mengambil
  ``HISTORY_ROWS`` baris terakhir yang tersedia — bukan "60 hari terakhir dari
  hari ini", karena dataset berhenti pada tanggal tertentu dan jendela
  "hari ini mundur 60 hari" bisa kosong padahal data cukup.
- **Kegagalan per-tick tidak mematikan loop**: satu site gagal (CSV hilang,
  model rusak) dicatat dan site berikutnya tetap diproses.

Lingkup: single-process (satu worker uvicorn, seperti `render.yaml`).
Bila service di-scale > 1 worker/replica, setiap proses menjalankan scheduler
sendiri — forecast identik (deterministik), tapi pemborosan CPU; solusi jangka
panjang: pindah ke antrean terpusat atau matikan scheduler di proses non-utama
(`SCHEDULER_ENABLED=false`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app import service
from app.actuals import ACTUALS_COLUMN, load_actual_series
from app.preprocessing import DEFAULT_TIMEZONE
from app.schemas import ForecastPoint
from app.storage import ForecastStore, StoredForecast, items_from_response, new_forecast_id

log = logging.getLogger("app.scheduler")

SITE_TZ = ZoneInfo(DEFAULT_TIMEZONE)

#: Regenerasi per jam — konsisten kontrak §1.4 (Cache-Control max-age=60,
#: "diregenerasi minimal per jam") dan SLA §5.2 "90 menit". Gate §10.5
#: (interval) sudah diputuskan di sini; retensi history masih terbuka.
DEFAULT_INTERVAL_SECONDS = 3600.0

#: Baris history yang dipakai scheduler. Artefak mensyaratkan >= 30
#: (``history_min_rows``); 60 memberi cadangan dan tetap murah (ARIMA harian).
DEFAULT_HISTORY_ROWS = 60

#: Horizon bawaan scheduler — sama dengan horizon utama evaluasi (§10.5).
DEFAULT_HORIZON = 7

#: Jendela muat yang sengaja lebar: tujuannya mengambil *tail* dataset,
#: bukan "rentang hari ini". CSV saat ini berhenti 2025-12-31 sementara
#: jam berjalan jauh setelahnya — jendela 60 hari dari "sekarang" akan kosong.
_HISTORY_LOOKBACK = timedelta(days=365 * 10)


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Baca `SCHEDULER_ENABLED` (default: aktif).

    Matikan dengan `SCHEDULER_ENABLED=false` — dipakai test dan proses
    worker sekunder (lihat modul docstring).
    """
    raw = (env if env is not None else os.environ).get("SCHEDULER_ENABLED", "true")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def interval_from_env(env: Mapping[str, str] | None = None) -> float:
    """Baca `SCHEDULER_INTERVAL_SECONDS` (default 3600; minimum 1 detik)."""
    raw = (env if env is not None else os.environ).get(
        "SCHEDULER_INTERVAL_SECONDS", ""
    ).strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning("SCHEDULER_INTERVAL_SECONDS=%r tidak valid; memakai default %ss",
                    raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    return max(value, 1.0)


def fetch_history(
    site_id: str,
    *,
    rows: int = DEFAULT_HISTORY_ROWS,
    now: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """``rows`` titik aktual terakhir sebagai ``(timestamp, value)`` untuk service.

    Mengambil tail dataset (bukan jendela "hari ini mundur N hari") supaya
    scheduler tetap menghasilkan forecast dari data terakhir yang benar-benar
    ada, walau dataset lebih tua dari jam sekarang.
    """
    if rows < 1:
        raise service.ForecastError(
            "E_VALIDATION_FAILED", f"rows harus >= 1 (dapat {rows})"
        )
    end = now or datetime.now(tz=SITE_TZ)
    if end.tzinfo is None or end.utcoffset() is None:
        raise service.ForecastError(
            "E_TZ_REQUIRED", "now harus tz-aware untuk memuat actuals"
        )
    start = end - _HISTORY_LOOKBACK
    frame = load_actual_series(site_id=site_id, start=start, end=end)
    tail = frame.tail(rows)
    if tail.empty:
        raise service.ForecastError(
            "E_HISTORY_TOO_SHORT",
            f"actuals kosong untuk {site_id} "
            f"({ACTUALS_COLUMN} tidak tersedia — jalankan scripts/prepare_actuals.py)",
        )
    return [
        (ts.to_pydatetime(), float(value))
        for ts, value in zip(tail.index, tail[ACTUALS_COLUMN])
    ]


def stored_from_outcome(
    outcome: service.ForecastOutcome,
    *,
    forecast_id: str | None = None,
) -> StoredForecast:
    """Konversi hasil service → objek simpan (poin melewati skema §3.1)."""
    points = tuple(
        ForecastPoint(
            forecast_time=p.forecast_time,
            forecast_value=p.forecast_value,
            model_version=p.model_version,
        )
        for p in outcome.points
    )
    return items_from_response(
        forecast_id=forecast_id or new_forecast_id(),
        site_id=outcome.site_id,
        target=outcome.target,
        resolution=outcome.resolution,
        unit=outcome.unit,
        ts=outcome.ts,
        generated_at=outcome.generated_at,
        model_version=outcome.model_version,
        points=points,
    )


def run_and_store_forecast(
    store: ForecastStore,
    *,
    site_id: str,
    history: Sequence[tuple[datetime, float]],
    horizon: int = DEFAULT_HORIZON,
    target: str = "irradiance",
    resolution: str = "daily",
    generated_at: datetime | None = None,
) -> StoredForecast:
    """Satu siklus penuh: `service.run_forecast` → `store.save`.

    Rantai persis sama dengan endpoint POST (kontrak §8). Kegagalan domain
    melempar ``service.ForecastError`` dengan kode kontrak.
    """
    outcome = service.run_forecast(
        site_id=site_id,
        history=history,
        horizon=horizon,
        target=target,
        resolution=resolution,
        generated_at=generated_at,
    )
    item = stored_from_outcome(outcome)
    store.save(item)
    return item


def run_site_forecast(
    store: ForecastStore,
    site_id: str,
    *,
    rows: int = DEFAULT_HISTORY_ROWS,
    horizon: int = DEFAULT_HORIZON,
    now: datetime | None = None,
) -> StoredForecast:
    """Ambil tail actuals site → forecast → simpan. Satu tick untuk satu site."""
    history = fetch_history(site_id, rows=rows, now=now)
    return run_and_store_forecast(
        store, site_id=site_id, history=history, horizon=horizon, generated_at=now
    )


def tick(
    store: ForecastStore,
    sites: Sequence[str],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Proses semua site sekali. Kegagalan per-site dicatat, tidak dibuang.

    Mengembalikan ``forecast_id`` yang berhasil disimpan (untuk log/uji).

    6·03 / 7·04 — jejak run: bila store mengimplementasikan
    ``record_run`` (penyedia DB), SETIAP run ditulis ke ``forecast_run``:
    sukses (forecast_id, generated_at, rows_saved) ATAU gagal (error).
    Tanpa itu kegagalan hanya hidup di log proses — hilang saat restart,
    dan "run gagal diam-diam" tak terdeteksi. Penyedia in-memory tanpa
    ``record_run`` dibiarkan tanpa catatan (PoC).
    """
    saved: list[str] = []
    for site_id in sites:
        started = now or datetime.now(tz=SITE_TZ)
        try:
            item = run_site_forecast(store, site_id, now=now)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:
            log.exception("scheduler tick gagal untuk site=%s — lanjut ke site berikut", site_id)
            _record_run(
                store,
                site_id=site_id,
                started_at=started,
                finished_at=datetime.now(tz=SITE_TZ),
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        saved.append(item.forecast_id)
        _record_run(
            store,
            site_id=site_id,
            started_at=started,
            finished_at=datetime.now(tz=SITE_TZ),
            status="ok",
            rows_saved=len(item.forecast),
            forecast_id=item.forecast_id,
            generated_at=item.generated_at,
            model_version=item.model_version,
        )
        log.info(
            "scheduler menyimpan forecast site=%s id=%s horizon=%s generated_at=%s",
            item.site_id, item.forecast_id, item.horizon,
            item.generated_at.isoformat(),
        )
    return saved


def _record_run(store: ForecastStore, **kwargs) -> None:
    """Catat run bila store mendukungnya (``record_run``); tanpa efek samping
    untuk penyedia in-memory. Kegagalan PENCATATAN tidak boleh menjatuhkan
    tick — sudah ditangani di dalam implementasi DB."""
    recorder = getattr(store, "record_run", None)
    if callable(recorder):
        try:
            recorder(**kwargs)
        except Exception:  # pragma: no cover - defensif ganda
            log.exception("run log gagal dicatat untuk site=%s", kwargs.get("site_id"))


async def scheduler_loop(
    store: ForecastStore,
    sites: Sequence[str],
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    immediate: bool = True,
    run_tick: Callable[..., list[str]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Loop tak berujing: tick → tidur interval → tick → ...

    ``immediate=True`` (default) menjalankan tick SEBELUM tidur pertama —
    dipakai saat test/loop mandiri. Lifespan memakai ``immediate=False``
    karena ia sudah menunggu tick pertama sendiri saat startup (lihat
    ``app/main.py``) — tanpa itu boot menghasilkan dua fit ARIMA dan dua
    entri history.

    ``run_tick`` / ``sleep`` bisa disuntikkan saat test (deterministik,
    tanpa tidur sungguhan). Pembatalan task (shutdown lifespan) menghentikan
    loop.
    """
    do_tick = run_tick or tick
    do_sleep = sleep or asyncio.sleep
    if immediate:
        await asyncio.to_thread(do_tick, store, sites)
    while True:
        await do_sleep(interval)
        await asyncio.to_thread(do_tick, store, sites)
