"""Isi nilai aktual otomatis setelah waktu tiba — Fase 6 · item 6·04.

Proses kecil di latar belakang: baca ``data/actuals.csv`` (lewat
:mod:`app.actuals` — satu-satunya pintu baca CSV) → sinkron ke
``actual_result`` → isi ``forecast_result.actual_value`` pada baris yang
``forecast_time``-nya sudah lewat dan masih NULL.

Tanpa langkah ini seluruh perhitungan akurasi Fase 8 selalu kosong
(checklist master 6·04).

Keputusan desain:

- **HANYA mengisi ``NULL``** — baris yang sudah terisi tidak pernah diubah;
  "diisi menyusul" bukan "ditimpa ulang". Koreksi data datang lewat
  ``actual_result`` (upsert per pengamatan), tetapi nilai forecast yang
  sudah di-backfill tidak diganti diam-diam.
- **Cutoff eksplisit** (default: ``site_now()``) — hanya baris dengan
  ``forecast_time < cutoff``; baris masa depan tetap ``NULL`` ("menunggu
  pengamatan").
- **Jalur hanya ada di penyedia DB** — tanpa ``DATABASE_URL`` (PoC
  in-memory) tidak ada yang diisi; akurasi berjalan memang butuh Fase 6.
- **Kegagalan tidak mematikan scheduler** — loop backfill meng-log lalu
  mencoba lagi pada siklus berikutnya (prinsip 7·04: kegagalan terlihat,
  bukan mematikan).

Jalankan otomatis bila ``DATABASE_URL`` di-set; bisa juga manual:

    python -m app.backfill

Interval: ``BACKFILL_INTERVAL_SECONDS`` (default 3600; sama dengan scheduler
— data aktual harian tak berubah lebih cepat dari itu).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Sequence

from app import actuals as actual_data
from app.actuals import ActualsUnavailable
from app.schemas import site_now

log = logging.getLogger("app.backfill")

#: Interval default — seirama dengan scheduler (3600s).
DEFAULT_INTERVAL_SECONDS = 3600.0

#: Jendela sinkron: 10 tahun ke belakang menutup dataset (mulai 2023)
#: dengan murah; ukuran CSV kecil, dilakukan per siklus.
SYNC_LOOKBACK_DAYS = 3650


def interval_from_env(env: dict[str, str] | None = None) -> float:
    """Baca ``BACKFILL_INTERVAL_SECONDS`` (default 3600; minimum 1)."""
    raw = (env if env is not None else os.environ).get(
        "BACKFILL_INTERVAL_SECONDS", ""
    )
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        log.warning("BACKFILL_INTERVAL_SECONDS=%r tidak valid; memakai default %ss",
                    raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    if value < 1:
        log.warning("BACKFILL_INTERVAL_SECONDS=%s < 1; memakai default", value)
        return DEFAULT_INTERVAL_SECONDS
    return value


def backfill_once(
    store,
    *,
    sites: Sequence[str],
    cutoff: datetime | None = None,
) -> dict[str, int]:
    """Satu siklus: sinkron aktual (per site) → isi baris yang sudah lewat.

    Mengembalikan ringkasan ``{"synced": n, "filled": m}`` untuk log/uji.
    Melempar ``ActualsUnavailable`` bila CSV tak terbaca — ditangkap pemanggil
    (loop) supaya kegagalan tercatat, bukan mematikan proses.
    """
    sync_method = getattr(store, "sync_actuals", None)
    fill_method = getattr(store, "fill_pending_actuals", None)
    if not callable(sync_method) or not callable(fill_method):
        # PoC in-memory: tidak ada tempat menyimpan aktual → tanpa aksi.
        log.debug("backfill: store tanpa sync_actuals/fill_pending_actuals — dilewati")
        return {"synced": 0, "filled": 0}

    now = cutoff or site_now()
    start = now - timedelta(days=SYNC_LOOKBACK_DAYS)

    total_synced = 0
    for site_id in sites:
        frame = actual_data.load_actual_series(site_id=site_id, start=start, end=now)
        rows = [
            # (site_id, ts, actual_value, unit, source)
            (
                site_id,
                ts.to_pydatetime(),
                float(row.actual_irradiance),
                "W/m2",
                actual_data.ACTUALS_SOURCE,
            )
            for ts, row in zip(frame.index, frame.itertuples())
        ]
        total_synced += sync_method(rows)

    filled = int(fill_method(cutoff=now))
    return {"synced": total_synced, "filled": filled}


async def backfill_loop(
    store,
    sites: Sequence[str],
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    immediate: bool = True,
    run: Callable[..., dict[str, int]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Loop tak berujung: backfill → tidur interval → backfill → ...

    Kegagalan satu siklus (CSV hilang, DB putus) di-log dan loop LANJUT —
    menunggu siklus berikutnya. ``run`` / ``sleep`` disuntikkan saat test.
    """
    do_run = run or backfill_once
    do_sleep = sleep or asyncio.sleep

    async def once() -> None:
        try:
            await asyncio.to_thread(do_run, store, sites=list(sites))
        except ActualsUnavailable as exc:
            log.error("backfill: data aktual tidak terbaca (%s) — mencoba lagi "
                      "pada siklus berikutnya", exc)
        except Exception:
            log.exception("backfill gagal — mencoba lagi pada siklus berikutnya")

    if immediate:
        await once()
    while True:
        await do_sleep(interval)
        await once()


def _main() -> int:
    """CLI sekali-jalan: ``python -m app.backfill`` (tanpa loop).

    Berguna untuk menjalankan backfill manual/berskala tunggal — misal setelah
    migrasi, atau di luar proses web (cron 7·03). Exit 0 bila sukses, 1 bila
    data aktual tak terbaca / store tanpa dukungan.
    """
    from app.db import PostgresForecastStore

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL tidak disetel — backfill butuh penyedia DB.")
        return 1
    sites = [s.strip() for s in os.environ.get("SITE_IDS", "PLTS_KUPANG").split(",") if s.strip()]
    store = PostgresForecastStore(dsn)
    try:
        summary = backfill_once(store, sites=sites)
    except ActualsUnavailable as exc:
        print(f"data aktual tidak terbaca: {exc}")
        return 1
    finally:
        store.close()
    print(f"backfill selesai: {summary['synced']} pengamatan disinkronkan, "
          f"{summary['filled']} baris forecast terisi aktualnya")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
