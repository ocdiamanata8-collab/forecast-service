"""Isi nilai aktual setelah waktu tiba — Fase 6 · item 6·04.

Job **sekali-jalan**: baca ``data/actuals.csv`` (lewat :mod:`app.actuals` —
satu-satunya pintu baca CSV) → sinkron ke ``actual_result`` → isi
``forecast_result.actual_value`` pada baris yang ``forecast_time``-nya sudah
lewat dan masih NULL.

Penjadwalan berulang dilakukan di luar proses web (GitHub Actions, lihat
``.github/workflows/forecast-jobs.yml``). Alasan: loop di dalam proses web
mati saat instance restart dan tidak terlihat di log deploy. CLI ini dapat
juga dijalankan manual setelah migrasi:

    python -m app.backfill

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
- **Kegagalan terlihat** — CLI exit nonzero bila DB/CSV tak tersedia;
  workflow eksternal menandai job gagal, bukan menyembunyikannya di log proses
  web.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Sequence

from app import actuals as actual_data
from app.actuals import ActualsUnavailable
from app.schemas import site_now

log = logging.getLogger("app.backfill")

#: Jendela sinkron: 10 tahun ke belakang menutup dataset (mulai 2023)
#: dengan murah; ukuran CSV kecil, dilakukan per siklus.
SYNC_LOOKBACK_DAYS = 3650


def backfill_once(
    store,
    *,
    sites: Sequence[str],
    cutoff: datetime | None = None,
) -> dict[str, int]:
    """Satu siklus: sinkron aktual (per site) → isi baris yang sudah lewat.

    Mengembalikan ringkasan ``{"synced": n, "filled": m}`` untuk log/uji.
    Melempar ``ActualsUnavailable`` bila CSV tak terbaca — ditangkap pemanggil
    (CLI) supaya kegagalan tercatat sebagai exit nonzero, bukan mematikan.
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


def _main() -> int:
    """CLI sekali-jalan: ``python -m app.backfill``.

    Dipakai job GitHub Actions & pemanggilan manual (cron 7·03). Exit 0 bila
    sukses, 1 bila DB tidak terjangkau / data aktual tak terbaca.
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
