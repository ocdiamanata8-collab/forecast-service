"""Uji determinisme dan kemurnian forecast_engine.

Berjalan sebagai script biasa (bukan pytest) supaya bisa dipanggil kapan pun:

    python test_forecast_engine.py

Tiga hal yang dibuktikan:
  1. Dua panggilan dengan input sama -> output bitwise identik.
  2. Tidak ada tulisan ke stdout (kecuali ringkasan akhir test ini sendiri).
  3. Tidak ada file yang dibuka saat fungsi berjalan.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.engine as fe  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def make_history(n: int = 400, seed: int = 0) -> pd.DataFrame:
    """Bangun history deterministik.

    Seed hanya untuk MEMBUAT data uji sekali di luar fungsi yang diuji;
    mesin forecast sendiri tidak pernah menyentuh RNG.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")

    day_of_year = idx.dayofyear.to_numpy()
    seasonal = 180 + 60 * np.sin(2 * np.pi * day_of_year / 365.25)
    noise = rng.normal(0, 12, n)

    return pd.DataFrame(
        {
            "irradiance": np.clip(seasonal + noise, 0, None),
            "temperature": 27 + 3 * np.sin(2 * np.pi * day_of_year / 365.25) + rng.normal(0, 0.5, n),
        },
        index=idx,
    )


def main() -> int:
    history = make_history()

    print("\n[1] DETERMINISME — dua panggilan, input identik")
    out_a = fe.generate_forecast(history, horizon=7)
    out_b = fe.generate_forecast(history, horizon=7)

    check(len(out_a) == 7, "jumlah baris == horizon")
    check(list(out_a.columns) == ["forecast_time", "forecast_value", "model_version"],
          "nama kolom sesuai kontrak")
    check(out_a.equals(out_b), "panggilan ke-1 dan ke-2 identik (DataFrame.equals)")
    check(out_a.to_json(date_format="iso") == out_b.to_json(date_format="iso"), "serialisasi JSON identik")
    check(
        np.array_equal(out_a["forecast_value"].to_numpy(), out_b["forecast_value"].to_numpy()),
        "nilai forecast bitwise identik",
    )
    from app.version import model_version as _current_version

    check((out_a["model_version"] == _current_version()).all(), "model_version terisi")

    print("\n[2] DETERMINISME — urutan pemanggilan dibalik")
    _ = fe.generate_forecast(history, horizon=14)
    out_c = fe.generate_forecast(history, horizon=7)
    check(out_a.equals(out_c), "hasil tetap sama setelah pemanggilan lain di antaranya")

    print("\n[3] DETERMINISME — salinan frame masukan (input tidak boleh dimodifikasi)")
    history_copy = history.copy(deep=True)
    out_d = fe.generate_forecast(history_copy, horizon=7)
    check(out_a.equals(out_d), "copy menghasilkan output sama")
    check(history.equals(history_copy), "frame input tidak berubah oleh fungsi")

    print("\n[4] KEMURNIAN — tidak ada tulisan ke stdout")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        fe.generate_forecast(history, horizon=7)
        fe.generate_forecast(history, weather_data=None, horizon=7)
        fe.evaluate_forecast([1.0, 2.0, 3.0], [1.1, 1.9, 3.2])
    check(buf.getvalue() == "", f"stdout/stderr kosong (dapat {len(buf.getvalue())} char)")

    print("\n[5] KEMURNIAN — tidak ada file dibuka saat fungsi berjalan")
    before = set(os.listdir("."))

    captured: list[object] = []
    real_open = open

    def spy_open(file, *args, **kwargs):  # noqa: ANN001
        captured.append(file)
        return real_open(file, *args, **kwargs)

    import builtins

    builtins.open = spy_open
    try:
        fe.generate_forecast(history, horizon=7)
    finally:
        builtins.open = real_open
    check(captured == [], f"builtin open tidak dipanggil (dapat {len(captured)})")
    check(set(os.listdir(".")) == before, "tidak ada file baru dibuat/dihapus")

    print("\n[6] KEMURNIAN — tanpa RNG, tanpa I/O di dalam kode yang dieksekusi")
    # Scan memakai AST supaya hanya kode yang benar-benar dieksekusi yang
    # diperiksa. Komentar dan docstring yang MENYEBUT pola terlarang (sebagai
    # aturan) tidak dihitung sebagai pelanggaran.
    import ast

    tree = ast.parse(Path(fe.__file__).read_text(encoding="utf-8"))

    banned_names = {"np.random", "random_state"}
    banned_calls = {"print", "open", "read_csv", "read_excel", "read_json", "seed"}
    banned_attrs = {"random", "seed", "default_rng"}

    offenders: list[str] = []
    for node in ast.walk(tree):
        # Abaikan docstring (Expr -> Constant str) agar aturan yang ditulis
        # di dalamnya tidak dianggap pelanggaran.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Name) and node.id in banned_names:
            offenders.append(f"Name:{node.id}")
        if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            offenders.append(f"Attr:{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in banned_calls:
            offenders.append(f"Call:{node.func.id}")

    offenders = sorted(set(offenders))
    check(offenders == [], f"kode dieksekusi bebas RNG/I/O (ditemukan: {offenders})")
    print("\n[7] VALIDASI KONTRAK — input yang melanggar harus ditolak")

    naive = history.copy()
    naive.index = naive.index.tz_localize(None)
    try:
        fe.generate_forecast(naive, horizon=7)
        check(False, "index naive (tanpa tz) ditolak")
    except ValueError:
        check(True, "index naive (tanpa tz) ditolak")

    try:
        fe.generate_forecast(history, horizon=0)
        check(False, "horizon 0 ditolak")
    except ValueError:
        check(True, "horizon 0 ditolak")

    try:
        fe.generate_forecast(history, horizon=31)
        check(False, "horizon 31 (melebihi kontrak) ditolak")
    except ValueError:
        check(True, "horizon 31 (melebihi kontrak) ditolak")

    try:
        fe.generate_forecast(history, horizon=7, target="tidak_ada")
        check(False, "kolom target hilang ditolak")
    except KeyError:
        check(True, "kolom target hilang ditolak")

    short = history.iloc[:10]
    try:
        fe.generate_forecast(short, horizon=7)
        check(False, "history < 30 baris ditolak")
    except ValueError:
        check(True, "history < 30 baris ditolak")

    gapped = history.drop(index=history.index[100])
    try:
        fe.generate_forecast(gapped, horizon=7)
        check(False, "history dengan gap waktu ditolak")
    except ValueError:
        check(True, "history dengan gap waktu ditolak")

    neg = history.copy()
    neg.loc[neg.index[5], "irradiance"] = -10
    try:
        fe.generate_forecast(neg, horizon=7)
        check(False, "nilai negatif ditolak")
    except ValueError:
        check(True, "nilai negatif ditolak")

    print("\n[8] KONTRAK OUTPUT — bentuk dan rentang")
    check(out_a["forecast_time"].is_monotonic_increasing, "forecast_time terurut naik")
    check(out_a["forecast_time"].is_unique, "forecast_time unik (C-04)")
    check(out_a["forecast_time"].dt.tz is not None, "forecast_time tz-aware (C-01)")
    check(str(out_a["forecast_time"].dt.tz) == "Asia/Makassar", "zona Asia/Makassar (C-02)")
    step = out_a["forecast_time"].iloc[1] - out_a["forecast_time"].iloc[0]
    check(step == pd.Timedelta(days=1), f"langkah 1 hari (didapat {step})")
    expected_start = history.index[-1] + pd.Timedelta(days=1)
    check(out_a["forecast_time"].iloc[0] == expected_start, "forecast dimulai tepat setelah history")
    # Nilai mentah: TANPA pembulatan, supaya identik dengan skrip asli
    # (tahap pemindahan: selisih nol). Pembulatan & klip negatif dilakukan
    # lapisan API (app/schemas.py, kontrak §3.1).
    check(fe.DECIMALS is None, "engine tidak membulatkan (DECIMALS dinonaktifkan)")

    print("\n[9] KONTRAK OUTPUT — determinisme dengan weather_data exog")
    weather = pd.DataFrame(
        {"cloud_cover": np.linspace(20, 60, len(history) + 7)},
        index=history.index.append(
            pd.date_range(history.index[-1] + pd.Timedelta(days=1), periods=7, freq="D",
                          tz="Asia/Makassar")
        ),
    )
    w_a = fe.generate_forecast(history, weather_data=weather, horizon=7,
                               exog_cols=["cloud_cover"])
    w_b = fe.generate_forecast(history, weather_data=weather, horizon=7,
                               exog_cols=["cloud_cover"])
    check(w_a.equals(w_b), "output dengan exog identik antar panggilan")
    check(list(w_a.columns) == list(out_a.columns), "skema sama dengan dan tanpa exog")

    try:
        fe.generate_forecast(history, weather_data=weather.iloc[:-7], horizon=7,
                             exog_cols=["cloud_cover"])
        check(False, "exog tanpa jendela penuh ditolak")
    except ValueError:
        check(True, "exog tanpa jendela penuh ditolak")

    print("\n[10] VARIAN LAIN — semua urutan zona deterministik")
    for zone, order in fe.MODEL_ORDERS.items():
        r1 = fe.generate_forecast(history, horizon=7, order=order)
        r2 = fe.generate_forecast(history, horizon=7, order=order)
        check(r1.equals(r2), f"{zone} ARIMA{order} deterministik")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN LULUS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
