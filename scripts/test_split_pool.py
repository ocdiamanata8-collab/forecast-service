"""UJI 6·05 — pembuktian least-privilege: pool baca TIDAK BISA menulis.

Hanya untuk container uji lokal (forecast-pg, port 15432).
Jalankan: python scripts/test_split_pool.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Repo root ke sys.path (pola sama dengan scripts lain) — tanpa pip install -e.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("API_KEYS", "dev-key-change-me-0123456789")

import psycopg  # noqa: E402

from app.db import PostgresForecastStore  # noqa: E402

WRITE = "postgresql://postgres:test@127.0.0.1:15432/forecast"
READ = "postgresql://reader:read@127.0.0.1:15432/forecast"
WITA = timezone(timedelta(hours=8))


def main() -> None:
    store = PostgresForecastStore(WRITE, read_dsn=READ)

    print(f"pool terpisah           : {store._read_pool is not store._write_pool}")

    latest = store.latest("PLTS_KUPANG")
    print(f"latest via read pool    : {latest is not None} "
          f"(pts={len(latest.forecast) if latest else 0})")

    # Pembuktian hak: INSERT lewat POOL BACA harus ditolak Postgres
    try:
        with store._read_pool.connection() as conn:
            conn.execute(
                "INSERT INTO forecast_result (forecast_id, site_id, target, "
                "resolution, unit, ts, forecast_time, generated_at, "
                "forecast_value, horizon_step, model_version) VALUES "
                "(gen_random_uuid(),'ZZ','irradiance','daily','W/m2',now(),"
                "now(),now(),1,1,'v0.0.1')"
            )
        print("LEAK: pool baca ternyata BISA menulis")
    except psycopg.errors.InsufficientPrivilege as e:
        first_line = str(e).splitlines()[0].strip()
        print(f"read pool MENOLAK tulis : {first_line}")

    # Jalur tulis tetap jalan
    from tests.test_db_store import _batch
    item, _ = _batch(datetime(2026, 10, 19, 6, 0, tzinfo=WITA))
    store.save(item)
    print(f"write pool MENULIS ok   : {store.latest('ZZ_TEST_DB') is not None}")

    # Run log juga lewat pool tulis
    store.record_run(
        site_id="ZZ_TEST_DB",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status="failed",
        error="uji least privilege",
    )
    with store._write_pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM forecast_run WHERE site_id='ZZ_TEST_DB'"
        ).fetchone()[0]
    print(f"record_run write ok     : {n == 1}")

    # Bersihkan
    with store._write_pool.connection() as conn:
        conn.execute("DELETE FROM forecast_run WHERE site_id='ZZ_TEST_DB'")
        conn.execute("DELETE FROM forecast_result WHERE site_id='ZZ_TEST_DB'")
    store.close()
    print("DONE")


if __name__ == "__main__":
    main()
