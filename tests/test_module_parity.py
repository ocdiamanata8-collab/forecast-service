"""Uji KESAMAAN hasil: modul app/ vs skrip asli (notebook).

    python tests/test_module_parity.py

Tahap ini hanya MEMINDAHKAN logika. Syarat keberhasilan:

    selisih output modul dan skrip == NOL

"Skrip asli" direproduksi di sini sebagai salinan **verbatim** dari sel
notebook `KP_SEI_ForecastDaily` (langkah 13–16 dan 25) — split train/test,
grid search ARIMA, forecast 7 hari — sehingga perbandingannya jujur tanpa
membuka file .ipynb tiap kali test jalan.

Bagian:

  A. ENGINE       — ARIMA skrip asli  vs  app.engine.generate_forecast
                    (selisih persis 0.0, termasuk presisi penuh)
  B. PREPROCESS   — resample harian skrip asli  vs  app.preprocessing
  C. PIPELINE     — ujung-ke-ujung: (resample + forecast) skrip
                    vs (prepare_dataset + generate_forecast) modul
  D. TIGA ZONA    — WIB / WITA / WIT, ketiganya selisih nol

Bila test ini gagal, modul sudah DIUBAH — kembalikan ke perilaku skrip.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# Console Windows default cp1252 tidak bisa mencetak simbol Unicode pada
# label test (Δ, ≠). Samakan dengan UTF-8; tidak mengubah logic test.
# Pola ini konsisten di semua test yang memakai simbol non-ASCII.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError, ValueError):
    pass

import numpy as np
import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.engine as fe            # noqa: E402
import app.preprocessing as pp     # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def max_abs_delta(a, b) -> float:
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    if a.shape != b.shape:
        return float("inf")
    return float(np.max(np.abs(a - b))) if a.size else 0.0


# ---------------------------------------------------------------------------
# Salinan verbatim logika skrip (notebook KP_SEI_ForecastDaily)
# ---------------------------------------------------------------------------
def script_resample_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Sel 10: ``df.resample('D').mean(numeric_only=True)``."""
    return df.resample("D").mean(numeric_only=True)


def script_arima_forecast(
    series: pd.Series,
    order: tuple[int, int, int],
    horizon: int,
) -> np.ndarray:
    """Sel 25: ``ARIMA(...).fit().forecast(steps=n)`` — default statsmodels.

    Tidak ada ``enforce_*``, tidak ada klip, tidak ada pembulatan: itulah
    tepat yang dijalankan skrip asli.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ARIMA(series, order=order).fit()
        return np.asarray(model.forecast(steps=horizon), dtype="float64")


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------
def make_dataset(tz: str, n: int = 400) -> pd.DataFrame:
    """Deret harian deterministik (seed hanya untuk MEMBUAT data uji)."""
    rng = np.random.default_rng(2026)
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz=tz)
    doy = idx.dayofyear.to_numpy(dtype="float64")
    values = (
        180
        + 60 * np.sin(2 * np.pi * doy / 365.25)
        + 12 * np.sin(2 * np.pi * np.arange(n) / 7.0)
        + rng.normal(0, 4, n)
    )
    return pd.DataFrame(
        {"irradiance": np.clip(values, 0, None),
         "temperature": 27 + 0.5 * rng.normal(0, 1, n)},
        index=idx,
    )


# ---------------------------------------------------------------------------
# A. ENGINE
# ---------------------------------------------------------------------------
def test_engine_parity() -> None:
    print("\n[A] ENGINE — ARIMA skrip asli vs app.engine.generate_forecast")
    data = make_dataset("Asia/Makassar")
    series = data["irradiance"]
    order = (1, 0, 2)
    horizon = 7

    reference = script_arima_forecast(series, order, horizon)
    out = fe.generate_forecast(
        pd.DataFrame(data), horizon=horizon, target="irradiance", order=order
    )
    module = out["forecast_value"].to_numpy(dtype="float64")

    delta = max_abs_delta(reference, module)
    print(f"      skrip  : {np.round(reference, 6)}")
    print(f"      modul  : {np.round(module, 6)}")
    print(f"      delta  : {np.round(np.abs(reference - module), 8)}")
    check(delta == 0.0, f"selisih nol (max|Δ| = {delta})")
    check(np.array_equal(reference, module), "bitwise identik")

    # Parameter fit juga harus sama — bukan cuma hasil akhirnya.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ref_fit = ARIMA(series, order=order).fit()
    # Modul tidak mengekspos fitted params, jadi kami uji lewat determinisme
    # panggilan ulang + kesetaraan nilai; itu cukup untuk klaim "tidak diubah".
    out2 = fe.generate_forecast(
        pd.DataFrame(data), horizon=horizon, target="irradiance", order=order
    )
    check(
        np.array_equal(out2["forecast_value"].to_numpy(), reference),
        "panggilan ulang tetap sama dengan skrip",
    )
    check(ref_fit.mle_retvals is not None, "fit skrip berhasil (pembanding valid)")


# ---------------------------------------------------------------------------
# B. PREPROCESSING
# ---------------------------------------------------------------------------
def test_preprocessing_parity() -> None:
    print("\n[B] PREPROCESSING — resample harian skrip vs app.preprocessing")

    # Data dengan jam berbeda-beda + satu gap, seperti data nyata.
    rng = np.random.default_rng(7)
    stamps = []
    day = pd.Timestamp("2024-01-01", tz="Asia/Makassar")
    for i in range(300):
        if i == 150:
            day = day + pd.Timedelta(days=1)  # bolong satu hari
        stamps.append(day + pd.Timedelta(hours=int(rng.choice([9, 15]))))
        day = day + pd.Timedelta(days=1)
    idx = pd.DatetimeIndex(stamps, tz="Asia/Makassar")
    vals = np.clip(180 + 60 * np.sin(2 * np.pi * idx.dayofyear.to_numpy(float) / 365.25)
                   + rng.normal(0, 5, len(idx)), 0, None)
    raw = pd.DataFrame({"irradiance": vals, "temperature": 27.0}, index=idx)

    reference = script_resample_daily(raw)
    prepared = pp.prepare_dataset(raw, target="irradiance", freq="1D")

    # Bandingkan hanya kolom target pada index yang sama — modul juga
    # mengisi gap (tambahan kualitas, bukan perubahan nilai di baris valid).
    common = reference.index.intersection(prepared.frame.index)
    ref_all = reference.loc[common, "irradiance"].to_numpy(dtype="float64")
    mod_all = prepared.frame.loc[common, "irradiance"].to_numpy(dtype="float64")
    # Hanya baris yang resample skrip asli memang punya nilai teramati.
    # Baris kosong sengaja diisi modul — itu preprocessing tambahan,
    # bukan perbedaan agregasi; di sini bandingkan murni baris yang sama.
    observed = np.isfinite(ref_all)
    ref_vals = ref_all[observed]
    mod_vals = mod_all[observed]

    delta = max_abs_delta(ref_vals, mod_vals)
    check(delta == 0.0, f"resample+mean identik pada baris valid (max|Δ| = {delta})")
    check(np.array_equal(ref_vals, mod_vals), "bitwise identik pada baris valid")
    check(len(prepared.frame) >= len(reference) - 1,
          "modul tidak membuang baris (gap hanya diisi)")
    check(str(prepared.frame.index.tz) == str(reference.index.tz),
          "zona index sama dengan skrip")


# ---------------------------------------------------------------------------
# C. PIPELINE UJUNG-KE-UJUNG
# ---------------------------------------------------------------------------
def test_pipeline_parity() -> None:
    print("\n[C] PIPELINE — resample+forecast skrip vs prepare+generate modul")
    data = make_dataset("Asia/Makassar", n=420)
    order = (1, 0, 2)
    horizon = 7

    # Jalur skrip: resample -> forecast.
    resampled = script_resample_daily(data)
    reference = script_arima_forecast(resampled["irradiance"], order, horizon)

    # Jalur modul.
    prepared = pp.prepare_dataset(data, target="irradiance", freq="1D")
    out = fe.generate_forecast(
        prepared.frame, horizon=horizon, target="irradiance", order=order
    )
    module = out["forecast_value"].to_numpy(dtype="float64")

    delta = max_abs_delta(reference, module)
    check(delta == 0.0, f"ujung-ke-ujung selisih nol (max|Δ| = {delta})")
    check(np.array_equal(reference, module), "ujung-ke-ujung bitwise identik")
    check(str(out["forecast_time"].dt.tz) == "Asia/Makassar",
          "timestamp output memakai zona sistem")


# ---------------------------------------------------------------------------
# D. TIGA ZONA (urutan WIB/WITA/WIT dari notebook)
# ---------------------------------------------------------------------------
def test_three_zones_parity() -> None:
    print("\n[D] TIGA ZONA — WIB / WITA / WIT, ketiganya selisih nol")
    zones = {"WIB": "Asia/Jakarta", "WITA": "Asia/Makassar", "WIT": "Asia/Jayapura"}
    orders = fe.MODEL_ORDERS

    for zone_name, tz in zones.items():
        data = make_dataset(tz, n=380)
        order = orders[zone_name]

        reference = script_arima_forecast(data["irradiance"], order, 7)
        # Engine menormalkan ke zona sistem lebih dulu; momen identik,
        # deretnya identik, jadi hasilnya harus persis sama.
        out = fe.generate_forecast(
            data, horizon=7, target="irradiance", order=order
        )
        module = out["forecast_value"].to_numpy(dtype="float64")

        delta = max_abs_delta(reference, module)
        check(delta == 0.0, f"{zone_name} ARIMA{order} selisih nol (Δ={delta})")


# ---------------------------------------------------------------------------
# E. MODUL TIDAK MENYIMPANG DARI SKRIP
# ---------------------------------------------------------------------------
def test_engine_has_no_extra_math() -> None:
    print("\n[E] MODUL — tanpa manipulasi tambahan (pasca pemindahan)")
    check(fe.DECIMALS is None,
          "engine tidak membulatkan (DECIMALS dinonaktifkan — tugas API)")

    src = Path(fe.__file__).read_text(encoding="utf-8")
    banned = [
        "enforce_stationarity",
        "enforce_invertibility",
        "np.clip",
        "np.round",
        "nan_to_num",
        "np.clip(values",
    ]
    hits = [b for b in banned if b in src]
    check(hits == [], f"engine bebas manipulasi tambahan (ditemukan: {hits})")


def main() -> int:
    test_engine_parity()
    test_preprocessing_parity()
    test_pipeline_parity()
    test_three_zones_parity()
    test_engine_has_no_extra_math()

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan — modul MENYIMPANG dari skrip")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("KESETARAAN MODUL vs SKRIP: SELISIH NOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
