"""Generator fixture regresi — dijalankan MANUAL, bukan oleh pytest.

    python tests/fixtures/make_fixtures.py

Menghasilkan dua berkas:

- ``forecast_input.csv``    input TETAP: history irradiance harian
- ``forecast_golden.csv``   output emas: hasil ``generate_forecast``
                            pada input itu dengan ``horizon=24``

Aturan:
- Data dibangun dari formula murni (tanpa RNG), sehingga input fixture
  identik setiap kali digenerate ulang.
- Refresh golden adalah keputusan EKSPLSIT: jalankan skrip ini, lalu
  periksa diff-nya. Jangan pernah menulis ulang golden otomatis saat test
  gagal — itu menghapus alarm yang justru dicari.
- Golden hanya memuat ``forecast_time`` dan ``forecast_value``.
  ``model_version`` sengaja DIKECUALIKAN: menaikkan ``model/VERSION`` adalah
  perubahan disengaja dan tidak boleh menggagalkan test regresi engine.
  Versi diuji terhadap file versi, bukan terhadap golden.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.preprocessing import DEFAULT_TIMEZONE  # noqa: E402

HERE = Path(__file__).resolve().parent
INPUT_CSV = HERE / "forecast_input.csv"
GOLDEN_CSV = HERE / "forecast_golden.csv"

N_ROWS = 120       # >= engine.MIN_HISTORY (30)
HORIZON = 24       # horizon yang dikunci test ini
FLOAT_FMT = "%.17g"  # presisi penuh — supaya ulang-generate identik bit-per-bit


def build_input() -> pd.DataFrame:
    """History irradiance harian deterministik — tanpa RNG.

    Musiman tahunan + mingguan, tanpa noise acak: kalau suatu hari test ini
    digenerate ulang, hasilnya identik. (Noise deterministik dari formula
    boleh, RNG tidak — input harus bisa direproduksi dari file semata.)
    """
    idx = pd.date_range("2024-01-01", periods=N_ROWS, freq="D", tz=DEFAULT_TIMEZONE)
    day_of_year = idx.dayofyear.to_numpy(dtype="float64")
    day_index = np.arange(N_ROWS, dtype="float64")

    irradiance = (
        185.0
        + 55.0 * np.sin(2 * np.pi * day_of_year / 365.25)   # musiman setahun
        + 14.0 * np.sin(2 * np.pi * day_index / 7.0)        # pola mingguan
        + 6.0 * np.cos(4 * np.pi * day_index / 30.0)        # pola ~bulanan
    )
    irradiance = np.clip(irradiance, 0.0, None)

    return pd.DataFrame({"irradiance": irradiance}, index=idx)


def run_engine(frame: pd.DataFrame) -> pd.DataFrame:
    """Jalankan engine dengan konfigurasi yang persis dipakai test."""
    return engine.generate_forecast(
        frame,
        horizon=HORIZON,
        target="irradiance",
    )


def main() -> int:
    frame = build_input()

    # Guard: kalau schema engine berubah, jangan menulis golden yang sudah
    # tidak sebanding — gagalkan dengan pesan yang jelas.
    expected_cols = ["forecast_time", "forecast_value", "model_version"]
    probe = run_engine(frame)
    if list(probe.columns) != expected_cols:
        print(f"GAGAL: kolom engine berubah: {list(probe.columns)}")
        print(f"       diharapkan: {expected_cols}")
        print("       Perbarui test_engine_golden.py dan fixture secara manual.")
        return 1

    # Input: timestamp + nilai (timestamp ditulis eksplisit agar tidak
    # bergantung pada format lokal pembaca).
    input_out = pd.DataFrame(
        {
            "timestamp": [ts.isoformat() for ts in frame.index],
            "irradiance": frame["irradiance"].to_numpy(dtype="float64"),
        }
    )
    input_out.to_csv(INPUT_CSV, index=False, float_format=FLOAT_FMT)

    # GOLDEN DIBUAT DARI JALUR YANG SAMA dengan test: baca ulang input CSV
    # dengan float_precision="round_trip". Bila golden dihasilkan dari frame
    # di memori sedangkan test membaca CSV, selisih parser float (~1e-14)
    # membesar lewat ARIMA sampai menggagalkan test tanpa ada perubahan
    # perilaku — alarm palsu yang persis ingin dihindari.
    reloaded = pd.read_csv(INPUT_CSV, float_precision="round_trip")
    timestamps = pd.to_datetime(reloaded["timestamp"], utc=False)
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(DEFAULT_TIMEZONE)
    else:
        timestamps = timestamps.dt.tz_convert(DEFAULT_TIMEZONE)
    frame_from_csv = pd.DataFrame(
        {"irradiance": reloaded["irradiance"].astype("float64").to_numpy()},
        index=pd.DatetimeIndex(timestamps),
    )
    golden = run_engine(frame_from_csv)

    if list(golden.columns) != expected_cols:
        print(f"GAGAL: kolom engine berubah: {list(golden.columns)}")
        print(f"       diharapkan: {expected_cols}")
        return 1

    # Golden: TANPA model_version (lihat docstring modul).
    golden_out = pd.DataFrame(
        {
            "forecast_time": [ts.isoformat() for ts in golden["forecast_time"]],
            "forecast_value": golden["forecast_value"].to_numpy(dtype="float64"),
        }
    )
    golden_out.to_csv(GOLDEN_CSV, index=False, float_format=FLOAT_FMT)

    print("Fixture ditulis:")
    print(f"  {INPUT_CSV.relative_to(ROOT)}  ({len(input_out)} baris input)")
    print(f"  {GOLDEN_CSV.relative_to(ROOT)}  ({len(golden_out)} baris golden)")
    print(f"  horizon = {HORIZON}   order = ARIMA{engine.DEFAULT_ORDER}")
    print(f"  model_version (TIDAK disimpan di golden): "
          f"{golden['model_version'].iloc[0]}")
    print()
    print("Selanjutnya: periksa diff lalu commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
