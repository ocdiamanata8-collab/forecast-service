"""Test regresi engine terhadap hasil emas (golden).

    python tests/test_engine_golden.py
    pytest tests/test_engine_golden.py -q

Peran test ini: **alarm**. Bila ada refactor yang tak sengaja mengubah
perilaku forecasting — bahkan di digit keenam — test ini gagal.

Isi fixture (lihat ``tests/fixtures/make_fixtures.py``):
- ``forecast_input.csv``   : 120 baris history irradiance harian, TETAP
- ``forecast_golden.csv``  : output ``generate_forecast(..., horizon=24)``
                             pada input itu

Catatan penting — apa yang TIDAK dibandingkan:
``model_version`` sengaja tidak ikut golden. Menaikkan ``model/VERSION``
adalah perubahan disengaja dan tidak boleh menggagalkan test regresi
(peringatan keliru). Versi diuji terpisah terhadap file ``model/VERSION``.

Cara menyegarkan golden (HANYA bila perubahan perilaku disengaja):

    python tests/fixtures/make_fixtures.py
    git diff tests/fixtures/forecast_golden.csv     # periksa dulu!

Jangan menyegarkan otomatis saat test gagal — itu mematikan alarmnya.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.preprocessing import DEFAULT_TIMEZONE  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUT_CSV = FIXTURES / "forecast_input.csv"
GOLDEN_CSV = FIXTURES / "forecast_golden.csv"

#: Horizon yang dikunci test ini. Ubah = harus segarkan fixture juga.
HORIZON = 24

#: Toleransi numerik: mengizinkan drift < 1e-6 relatif (noise float lingkungan),
#: tetapi menangkap setiap perubahan perilaku yang bermakna.
RTOL = 1e-6

#: Kolom yang dibandingkan terhadap golden.
COMPARED_COLUMNS = ["forecast_time", "forecast_value"]

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def load_input() -> pd.DataFrame:
    """Baca fixture input menjadi frame siap engine."""
    if not INPUT_CSV.exists():
        raise FileNotFoundError(
            f"fixture input tidak ada: {INPUT_CSV}\n"
            "Jalankan: python tests/fixtures/make_fixtures.py"
        )
    # float_precision="round_trip" WAJIB: parser float default pandas bukan
    # round-trip eksak (selisih ~1e-14 per baris). Lewat ARIMA, selisih
    # sekecil itu membesar jadi 0.02 pada horizon 24 dan menggagalkan test
    # tanpa ada perubahan perilaku sama sekali.
    raw = pd.read_csv(INPUT_CSV, float_precision="round_trip")
    timestamps = pd.to_datetime(raw["timestamp"], utc=False)
    # Fixture ditulis dengan offset eksplisit; paksa zona sistem bila parser
    # kehilangan tz (pandas bisa membuang tz pada kolom campuran).
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(DEFAULT_TIMEZONE)
    else:
        timestamps = timestamps.dt.tz_convert(DEFAULT_TIMEZONE)
    # .to_numpy() WAJIB: melewatkan Series yang masih membawa RangeIndex ke
    # pembuatan frame dengan DatetimeIndex membuat pandas REINDEX — label
    # tidak cocok, seluruh nilai berubah NaN tanpa error.
    frame = pd.DataFrame(
        {"irradiance": raw["irradiance"].astype("float64").to_numpy()},
        index=pd.DatetimeIndex(timestamps),
    )
    if frame["irradiance"].isna().any():
        raise ValueError(
            f"fixture input menghasilkan {int(frame['irradiance'].isna().sum())} NaN — "
            "periksa pembacaan fixture (ind Series tidak boleh ikut di-reindex)"
        )
    return frame


def load_golden() -> pd.DataFrame:
    """Baca fixture golden. Kolom ``model_version`` sengaja tidak dibaca."""
    if not GOLDEN_CSV.exists():
        raise FileNotFoundError(
            f"fixture golden tidak ada: {GOLDEN_CSV}\n"
            "Jalankan: python tests/fixtures/make_fixtures.py"
        )
    raw = pd.read_csv(GOLDEN_CSV, float_precision="round_trip")
    expected = ["forecast_time", "forecast_value"]
    missing = [c for c in expected if c not in raw.columns]
    if missing:
        raise ValueError(
            f"fixture golden tidak punya kolom {missing}; ditemukan {list(raw.columns)}. "
            "Schema golden berubah — perbarui fixture secara manual, jangan otomatis."
        )
    times = pd.to_datetime(raw["forecast_time"], utc=False)
    if times.dt.tz is None:
        times = times.dt.tz_localize(DEFAULT_TIMEZONE)
    else:
        times = times.dt.tz_convert(DEFAULT_TIMEZONE)
    return pd.DataFrame(
        {
            "forecast_time": pd.DatetimeIndex(times),
            "forecast_value": raw["forecast_value"].astype("float64"),
        }
    )


def test_engine_matches_golden() -> None:
    """Jantung test ini: output engine == hasil emas.

    Berupa fungsi bernama ``test_*`` supaya dijalankan pytest DAN bisa
    dipanggil ulang oleh ``main()`` untuk mode script.
    """
    from pandas.testing import assert_frame_equal

    fixture = load_input()
    golden = load_golden()

    got = engine.generate_forecast(fixture, horizon=HORIZON, target="irradiance")
    got = got[COMPARED_COLUMNS].reset_index(drop=True)
    expected = golden[COMPARED_COLUMNS].reset_index(drop=True)

    # Bentuk dulu — kalau panjang berubah, pesan lebih jelas daripada
    # error perbandingan nilai.
    assert len(got) == len(expected), (
        f"jumlah baris berubah: dapat {len(got)}, emas {len(expected)}. "
        "Horizon/aturan jendela forecast berubah."
    )
    assert list(got.columns) == list(expected.columns), (
        f"kolom berubah: dapat {list(got.columns)}, emas {list(expected.columns)}"
    )

    assert_frame_equal(
        got,
        expected,
        check_exact=False,
        rtol=RTOL,
        atol=1e-9,
        check_dtype=False,  # CSV memakai float64; engine boleh float64/np.float64
        obj="forecast engine vs hasil emas",
    )


def main() -> int:
    print(f"\n[GOLDEN] horizon={HORIZON}  rtol={RTOL}  order=ARIMA{engine.DEFAULT_ORDER}")

    fixture = load_input()
    golden = load_golden()

    print("\n[1] FIXTURE TERBACA")
    check(len(fixture) == 120, f"input 120 baris (dapat {len(fixture)})")
    check(str(fixture.index.tz) == DEFAULT_TIMEZONE,
          f"zona input {fixture.index.tz}")
    check(len(golden) == HORIZON, f"golden {HORIZON} baris (dapat {len(golden)})")
    check("forecast_value" in golden.columns, "golden memuat forecast_value")
    check((golden["forecast_value"] > 0).all(), "golden seluruhnya positif")

    print("\n[2] OUTPUT ENGIN == HASIL EMAS")
    try:
        test_engine_matches_golden()
        check(True, f"assert_frame_equal lulus (rtol={RTOL})")
    except AssertionError as exc:
        check(False, f"SELISIH DENGAN GOLDEN: {str(exc)[:400]}")

    print("\n[3] DETERMINISME — dua run identik")
    a = engine.generate_forecast(fixture, horizon=HORIZON, target="irradiance")
    b = engine.generate_forecast(fixture, horizon=HORIZON, target="irradiance")
    check(a.equals(b), "hasil run ke-2 identik bitwise")

    print("\n[4] VERSI DIUJI TERHADAP BUKAN GOLDEN")
    # model_version tidak ikut golden (sengaja) — uji ke file versinya.
    from app.version import model_version

    version = model_version()
    version_file = (ROOT / "model" / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    check(version == version_file, f"model_version() == baris file ({version})")
    check(a["model_version"].nunique() == 1, "satu versi untuk semua baris")
    check(a["model_version"].iloc[0] == version,
          f"output engine memakai versi file ({a['model_version'].iloc[0]})")
    check("model_version" not in golden.columns,
          "golden TIDAK memuat model_version (peringatan keliru dicegah)")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan — perilaku engine BERUBAH")
        for f in FAILURES:
            print(f"  - {f}")
        print()
        print("Bila perubahan ini SENGAJA:")
        print("  python tests/fixtures/make_fixtures.py")
        print("  git diff tests/fixtures/forecast_golden.csv    # periksa!")
        print("Bila TIDAK sengaja: kembalikan perilaku engine.")
        return 1
    print("ENGINE == GOLDEN — tidak ada drift perilaku")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
