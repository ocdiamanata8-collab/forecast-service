"""Sumber data aktual per-titik — loader server-side saja.

Sumber kebenaran CSV di dalam service: ``data/actuals.csv`` (disiapkan oleh
``scripts/prepare_actuals.py``). Tidak pernah membaca CSV proyek langsung;
endpoint hanya mengembalikan field source generik, tidak path filesystem.

Target kolom: ``actual_irradiance`` dalam W/m² (Solcast harian). Jangan
mengganti dengan ``ghi_cams``: satuannya Wh/m² per hari (×24 berbeda).

Loader ini pengecualian yang terdokumentasi untuk aturan kemurnian
``app/engine.py`` — hanya modul akses data yang boleh membaca file; engine
forecast tetap murni. Cache di-memoisasi berdasarkan mtime file supaya
refresh actuals membuat cache otomatis invalid.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.preprocessing import DEFAULT_TIMEZONE

ACTUALS_FILE = Path(__file__).resolve().parents[1] / "data" / "actuals.csv"
ACTUALS_SOURCE = "data/actuals.csv"
ACTUALS_COLUMN = "actual_irradiance"
SITE_TZ = ZoneInfo(DEFAULT_TIMEZONE)


class ActualsUnavailable(RuntimeError):
    """Sumber actuals hilang/tidak dapat dibaca — API memetakannya ke 503."""


def _mtime_ns() -> int:
    if not ACTUALS_FILE.exists():
        raise ActualsUnavailable(
            "Actual irradiance data is not installed. Run "
            "`python scripts/prepare_actuals.py` then retry."
        )
    try:
        return ACTUALS_FILE.stat().st_mtime_ns
    except OSError as exc:
        raise ActualsUnavailable(f"Actual irradiance data is unavailable: {exc}") from exc


@lru_cache(maxsize=2)
def _load_frame(mtime_ns: int) -> pd.DataFrame:
    """Baca file aktual sekali per mtime, verifikasi skema, dan kunci index tz-aware."""
    # mtime_ns sengaja jadi key cache agar perubahan file refresh otomatis.
    _ = mtime_ns
    try:
        frame = pd.read_csv(ACTUALS_FILE, parse_dates=["tanggal"])
    except (OSError, pd.errors.ParserError, ValueError) as exc:
        raise ActualsUnavailable(
            "Actual irradiance data could not be read. Rebuild it with "
            "`python scripts/prepare_actuals.py`."
        ) from exc

    missing = {"tanggal", ACTUALS_COLUMN} - set(frame.columns)
    if missing:
        raise ActualsUnavailable(
            "Actual irradiance data has an invalid schema. Rebuild it with "
            "`python scripts/prepare_actuals.py`."
        )

    frame = frame[["tanggal", ACTUALS_COLUMN]].copy()
    frame[ACTUALS_COLUMN] = pd.to_numeric(frame[ACTUALS_COLUMN], errors="coerce")
    frame = frame.dropna(subset=["tanggal", ACTUALS_COLUMN])
    frame = frame.sort_values("tanggal").drop_duplicates(subset=["tanggal"], keep="last")
    if (frame[ACTUALS_COLUMN] < 0).any():
        raise ActualsUnavailable("Actual irradiance data contains negative values.")

    idx = pd.DatetimeIndex(frame["tanggal"])
    # CSV source contains calendar days without offset. Its contract is explicit:
    # these dates are local Kupang calendar days (Asia/Makassar), not guessed.
    if idx.tz is None:
        idx = idx.tz_localize(DEFAULT_TIMEZONE)
    else:
        idx = idx.tz_convert(DEFAULT_TIMEZONE)
    frame = frame.drop(columns=["tanggal"])
    frame.index = idx
    frame.index.name = "timestamp"
    return frame


def load_actual_series(
    *,
    site_id: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Muat titik actual untuk site dan rentang inklusif.

    Validasi site (`E_SITE_UNKNOWN`) sudah dilakukan endpoint lewat
    ``assert_known_site`` — fungsi ini tidak mengulanginya, karena registry
    site dapat dikonfigurasi lewat env ``SITE_IDS`` dan loader tidak boleh
    punya daftar site sendiri. File CSV saat ini berisi data satu site
    (PLTS_KUPANG); menambah site = menambah baris/kolom di CSV, bukan
    mengubah loader.

    Rentang wajib tz-aware sebelum fungsi dipanggil. Output terurut dengan
    index ``Asia/Makassar``; NaN tidak dikembalikan.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("from must include an explicit timezone offset")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("to must include an explicit timezone offset")
    if end.astimezone(timezone.utc) < start.astimezone(timezone.utc):
        raise ValueError("to must be greater than or equal to from")

    frame = _load_frame(_mtime_ns())
    start_local = start.astimezone(SITE_TZ)
    end_local = end.astimezone(SITE_TZ)
    result = frame.loc[(frame.index >= start_local) & (frame.index <= end_local)].copy()
    return result


def clear_actuals_cache() -> None:
    """Khusus test / script yang menukar sumber pada proses yang sama."""
    _load_frame.cache_clear()
