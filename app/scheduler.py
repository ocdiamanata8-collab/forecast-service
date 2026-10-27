"""Scheduler rolling forecast — pengisi `latest`/`history` latar belakang.

Kontrak §5.1: `POST /forecast` **tidak menyimpan** hasil; "penyimpanan terjadi
di scheduler latar belakang". Modul itulah scheduler itu (dampak tanpanya:
`GET /forecast/latest` selalu 404 `E_NO_FORECAST`).

KEPUTUSAN PENJADWALAN (lihat README "Keputusan interval"):
**loop tak berujung TIDAK dijalankan di dalam proses web.** Regenerasi per jam
dijadwalkan oleh mekanisme server eksternal (GitHub Actions — gratis untuk repo
public; lihat `.github/workflows/forecast-jobs.yml`) yang memanggil
`POST /internal/v1/jobs/forecast` atau wrapper `scripts/run_forecast.sh`.
Alasan: loop di dalam proses web mati saat instance restart dan tidak terlihat
di log deploy. Yang TETAP di sini:

- **Warm tick sekali saat startup** (sinkron, bukan loop) — `SCHEDULER_ENABLED`
  mengendalikan ini; supaya `GET /forecast/latest` langsung 200 setelah deploy.
- **Satu rantai fungsi** untuk semua jalur (kontrak §8): warm tick, endpoint
  job, dan cron semuanya lewat `run_site_forecast` → `service.run_forecast`.

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
- **Kegagalan per-site tidak mematikan tick**: satu site gagal (CSV hilang,
  model rusak) dicatat (termasuk ke ``forecast_run``) dan site berikutnya tetap
  diproses.

Lingkup: tanpa proses scheduler di dalam aplikasi, skala multi-worker/replica
tidak lagi menggandakan regenerasi — pekerjaan berulang hidup di scheduler
eksternal sekali saja.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from app import service
from app.actuals import ACTUALS_COLUMN, load_actual_series
from app.preprocessing import DEFAULT_TIMEZONE
from app.schemas import ForecastPoint
from app.storage import ForecastStore, StoredForecast, items_from_response, new_forecast_id

log = logging.getLogger("app.scheduler")

SITE_TZ = ZoneInfo(DEFAULT_TIMEZONE)

#: Baris history yang dipakai scheduler. Artefak mensyaratkan >= 30
#: (``history_min_rows``); 60 memberi cadangan dan tetap murah (ARIMA harian).
DEFAULT_HISTORY_ROWS = 60

#: Horizon bawaan scheduler/cron — satu nilai untuk warm tick + job eksternal.
#: 24 hari mengikuti endpoint job dan keputusan operasional README. Berbeda
#: dengan horizon primary evaluasi (7): itu metrik, bukan jendela forecast.
DEFAULT_HORIZON = 24

#: Jendela muat yang sengaja lebar: tujuannya mengambil *tail* dataset,
#: bukan "rentang hari ini". CSV saat ini berhenti 2025-12-31 sementara
#: jam berjalan jauh setelahnya — jendela 60 hari dari "sekarang" akan kosong.
_HISTORY_LOOKBACK = timedelta(days=365 * 10)


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Baca `SCHEDULER_ENABLED` (default: aktif).

    `true` = jalankan warm tick sekali saat startup (tanpa loop); `false` = web
    tanpa kerja forecast sama sekali (dipakai test dan worker sekunder).
    Interval regenerasi tidak lagi milik env — dijadwalkan scheduler eksternal
    (GitHub Actions; lihat README "Keputusan interval").
    """
    raw = (env if env is not None else os.environ).get("SCHEDULER_ENABLED", "true")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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
    """Ambil tail actuals site → forecast → simpan. Satu run untuk satu site.

    6·03 / 7·04 — jejak run: bila store mengimplementasikan ``record_run``
    (penyedia DB), SETIAP run ditulis ke ``forecast_run`` — sukses
    (forecast_id, generated_at, rows_saved) ATAU gagal (error). Pencatatan ada
    DI SINI (bukan di pemanggil): endpoint job, warm tick, dan cron eksternal
    semuanya melewati fungsi ini, sehingga run luar punya jejak yang sama;
    tanpa itu kegagalan run eksternal hanya hidup di log runner CI dan hilang
    saat restart. Pencatatan tidak boleh menjatuhkan run (defensif ganda:
    implementasi DB sudah menangkapnya).
    """
    started = now or datetime.now(tz=SITE_TZ)
    try:
        history = fetch_history(site_id, rows=rows, now=now)
        item = run_and_store_forecast(
            store, site_id=site_id, history=history, horizon=horizon, generated_at=now
        )
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise
    except Exception as exc:
        _record_run(
            store,
            site_id=site_id,
            started_at=started,
            finished_at=datetime.now(tz=SITE_TZ),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
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
    return item


def tick(
    store: ForecastStore,
    sites: Sequence[str],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Proses semua site sekali. Kegagalan per-site dicatat, tidak dibuang.

    Mengembalikan ``forecast_id`` yang berhasil disimpan (untuk log/uji).
    Jejak run ke ``forecast_run`` dilakukan di dalam
    :func:`run_site_forecast` — lihat catatan di sana.
    """
    saved: list[str] = []
    for site_id in sites:
        try:
            item = run_site_forecast(store, site_id, now=now)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:
            log.exception("scheduler tick gagal untuk site=%s — lanjut ke site berikut", site_id)
            continue
        saved.append(item.forecast_id)
        log.info(
            "scheduler menyimpan forecast site=%s id=%s horizon=%s generated_at=%s",
            item.site_id, item.forecast_id, item.horizon,
            item.generated_at.isoformat(),
        )
    return saved


def _record_run(store: ForecastStore, **kwargs) -> None:
    """Catat run bila store mendukungnya (``record_run``); tanpa efek samping
    untuk penyedia in-memory. Kegagalan PENCATATAN tidak boleh menjatuhkan
    run — sudah ditangani di dalam implementasi DB dan di sini defensif."""
    recorder = getattr(store, "record_run", None)
    if callable(recorder):
        try:
            recorder(**kwargs)
        except Exception:  # pragma: no cover - defensif ganda
            log.exception("run log gagal dicatat untuk site=%s", kwargs.get("site_id"))
