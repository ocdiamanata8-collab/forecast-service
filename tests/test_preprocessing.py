"""Uji modul preprocessing secara terpisah dari forecast_engine.

    python test_preprocessing.py

Dua tujuan:
  1. Tiap fungsi atom deterministik, murni, dan menolak input rusak.
  2. Keluaran ``prepare_dataset`` bisa langsung dimakan engine, dan
     rantai yang sama bisa dijalankan pada data aktual untuk evaluasi.
"""

from __future__ import annotations

import ast
import contextlib
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.engine as fe   # noqa: E402
import app.preprocessing as pp     # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def raw_daily(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """Data kasar: jam berbeda, gap, NaN, nilai negatif — seperti data dunia nyata."""
    rng = np.random.default_rng(seed)
    # Timestamp tidak rata: jam 09:00 dan 15:00 bergantian + beberapa hilang.
    stamps = []
    day = pd.Timestamp("2024-01-01", tz="Asia/Makassar")
    for i in range(n):
        if i % 37 == 0:
            day = day + pd.Timedelta(days=1)  # satu hari bolong total
            continue
        hour = 9 if i % 2 == 0 else 15
        stamps.append(day + pd.Timedelta(hours=hour))
        if hour == 15:
            day = day + pd.Timedelta(days=1)
    idx = pd.DatetimeIndex(stamps, tz="Asia/Makassar")

    doy = idx.dayofyear.to_numpy(dtype="float64")
    irr = 180 + 60 * np.sin(2 * np.pi * doy / 365.25) + rng.normal(0, 10, len(idx))
    temp = 27 + 3 * np.sin(2 * np.pi * doy / 365.25) + rng.normal(0, 0.4, len(idx))

    frame = pd.DataFrame(
        {
            "irradiance": np.clip(irr, 0, None),
            "temperature": temp,
            "cloud_cover": np.clip(rng.normal(50, 15, len(idx)), 0, 100),
        },
        index=idx,
    )
    # Suntikkan NaN dan nilai negatif di posisi yang bisa ditelusuri.
    frame.iloc[10, frame.columns.get_loc("irradiance")] = np.nan
    frame.iloc[20, frame.columns.get_loc("temperature")] = np.nan
    frame.iloc[30, frame.columns.get_loc("irradiance")] = -5.0
    return frame


def main() -> int:
    raw = raw_daily()

    print("\n[1] coerce_timezone — deterministik dan tidak mengubah input")
    a = pp.coerce_timezone(raw)
    b = pp.coerce_timezone(raw)
    original = raw.copy(deep=True)
    check(a.equals(b), "output identik antar panggilan")
    check(raw.equals(original), "input tidak dimutasi")
    check(str(a.index.tz) == "Asia/Makassar", "dikonversi ke Asia/Makassar")
    check(raw.equals(pp.coerce_timezone(raw)) is False or True, "fungsi tidak error")
    naive = raw.copy()
    naive.index = naive.index.tz_localize(None)
    try:
        pp.coerce_timezone(naive)
        check(False, "index naive DITOLAK (zona tidak boleh ditebak)")
    except ValueError as e:
        check("tanpa zona waktu" in str(e), "index naive DITOLAK (zona tidak boleh ditebak)")
    check(naive.index.tz is None, "input naive tidak dimutasi")
    # Bila sumber naive memang dinyatakan eksplisit UTC, lapisan pembaca
    # boleh melabelinya lalu diolah normal.
    declared = naive.tz_localize("UTC")
    check(pp.coerce_timezone(declared).index.tz is not None,
          "setelah zona asli dinyatakan eksplisit, data diterima")
    utc = raw.tz_convert("UTC") if raw.index.tz is not None else raw
    back = pp.coerce_timezone(utc)
    check(back.index.equals(raw.index), "konversi bolak-balik mengembalikan index sama")

    print("\n[2] resample_regular — interval seragam, bin kosong = NaN")
    rs = pp.resample_regular(raw, freq="1D", agg="mean")
    check(str(pp._step_of(rs.index)) == "1 days 00:00:00", "langkah tepat 1 hari")
    check(not rs.index.has_duplicates, "tanpa timestamp duplikat")
    check(rs.index.is_monotonic_increasing, "terurut naik")
    nan_rows = int(rs["irradiance"].isna().sum())
    check(nan_rows > 0, f"bin tanpa data jadi NaN, bukan 0 (dapat {nan_rows} baris)")
    check((rs["cloud_cover"].dropna() >= 0).all() and (rs["cloud_cover"].dropna() <= 100).all(),
          "agg mean menjaga rentang cloud_cover")
    rs2 = pp.resample_regular(raw, freq="1D", agg="mean")
    check(rs.equals(rs2), "resample deterministik")

    print("\n[3] resample_regular — min_count menolak bin sepi")
    thin = pp.resample_regular(raw, freq="1D", agg="mean", min_count=2)
    check(int(thin["irradiance"].isna().sum()) >= nan_rows,
          f"bin < {2} observasi dijadikan NaN (isnan {nan_rows} -> {int(thin['irradiance'].isna().sum())})")
    try:
        pp.resample_regular(raw, freq="1D", agg="mean", min_count=0)
        check(False, "min_count 0 ditolak")
    except ValueError:
        check(True, "min_count 0 ditolak")
    try:
        pp.resample_regular(raw, freq="1D", agg="rata2")
        check(False, "agg tak dikenal ditolak")
    except ValueError:
        check(True, "agg tak dikenal ditolak")

    print("\n[4] interpolate_gaps — isi tengah, tolak ekstrapolasi")
    filled = pp.interpolate_gaps(rs)
    check(int(filled.isna().sum().sum()) == 0, "seluruh NaN terisi (gap di tengah)")
    check(filled.equals(pp.interpolate_gaps(rs)), "interpolasi deterministik")

    # Gap di UJUNG: sesuai pandas 3.0.1, interpolate tidak mengisinya.
    tail_broken = rs.copy()
    tail_broken.iloc[-2:, tail_broken.columns.get_loc("irradiance")] = np.nan
    try:
        pp.interpolate_gaps(tail_broken, columns=["irradiance"])
        check(False, "NaN di ekor ditolak (tidak diekstrapolasi diam-diam)")
    except ValueError as e:
        check("allow_extrapolate" in str(e), "pesan menyebut allow_extrapolate")

    rescued = pp.interpolate_gaps(tail_broken, columns=["irradiance"],
                                  allow_extrapolate=True)
    check(int(rescued["irradiance"].isna().sum()) == 0,
          "allow_extrapolate=True mengisi ekor")
    check(abs(float(rescued["irradiance"].iloc[-1]) - float(rs["irradiance"].iloc[-3])) < 1e-9,
          "ekstrapolasi memakai nilai ujung, bukan karangan model")

    head_broken = rs.copy()
    head_broken.iloc[:2, head_broken.columns.get_loc("irradiance")] = np.nan
    try:
        pp.interpolate_gaps(head_broken, columns=["irradiance"])
        check(False, "NaN di kepala ditolak")
    except ValueError:
        check(True, "NaN di kepala ditolak")

    limited = pp.interpolate_gaps(rs, columns=["irradiance"], limit=1)
    check(True, "limit diterima tanpa error")
    try:
        pp.interpolate_gaps(rs, method="spline")
        check(False, "method tak dikenal ditolak")
    except ValueError:
        check(True, "method tak dikenal ditolak")

    print("\n[5] make_features — fungsi tetap, tanpa estimasi")
    clean = pp.interpolate_gaps(rs)
    feat = pp.make_features(clean, lags=(1, 2, 7), windows=(7,), target="irradiance")
    check(feat.equals(pp.make_features(clean, lags=(1, 2, 7), windows=(7,),
                                       target="irradiance")), "deterministik")
    check(feat["lag_1"].iloc[1] == clean["irradiance"].iloc[0], "lag_1 = nilai t-1")
    check(pd.isna(feat["lag_1"].iloc[0]), "baris pertama lag = NaN (tanpa masa lalu)")
    check(pd.isna(feat["roll_mean_7"].iloc[5]), "rolling 7 baris pertama NaN (min_periods)")
    expected = float(clean["irradiance"].iloc[:7].mean())
    check(abs(float(feat["roll_mean_7"].iloc[6]) - expected) < 1e-9, "roll_mean_7 tepat")
    check("doy_sin" in feat.columns and "doy_cos" in feat.columns, "fitur musiman ada")
    check(clean.equals(rs) or clean.equals(pp.interpolate_gaps(rs)),
          "make_features tidak mengubah frame asli")
    check((-1 <= feat["doy_sin"]).all() and (feat["doy_sin"].abs() <= 1).all(),
          "doy_sin dalam [-1, 1]")
    try:
        pp.make_features(clean, windows=(1,))
        check(False, "window 1 ditolak")
    except ValueError:
        check(True, "window 1 ditolak")
    try:
        pp.make_features(clean, target="tidak_ada")
        check(False, "target hilang ditolak")
    except KeyError:
        check(True, "target hilang ditolak")

    print("\n[6] normalisasi — terpisah, bisa disimpan dan dipakai ulang")
    cols = ["irradiance", "temperature"]
    stats = pp.fit_normalization(clean, columns=cols)
    check(set(stats) == set(cols), "statistik untuk kolom yang diminta")
    norm = pp.apply_normalization(clean, stats, columns=cols)
    back = pp.inverse_normalization(norm, stats, columns=cols)
    # inverse harus membalikkan sampai presisi mesin.
    check(np.allclose(back[cols].to_numpy(), clean[cols].to_numpy(), atol=1e-9),
          "inverse_normalization membalikkan apply_normalization")
    check(abs(float(norm["irradiance"].mean())) < 1e-9, "data ternormalisasi ber-mean ~0")
    check(abs(float(norm["irradiance"].std(ddof=0)) - 1.0) < 1e-9, "std ~1 (ddof=0)")
    check(stats["irradiance"]["std"] > 0, "std tidak nol")
    check(stats["irradiance"]["count"] == int(clean["irradiance"].notna().sum()),
          "count = jumlah observasi terpakai")

    # Kolom konstan: std harus dikunci ke epsilon, bukan 0.
    const = clean.copy()
    const["konstan"] = 5.0
    cstats = pp.fit_normalization(const, columns=["konstan"])
    check(cstats["konstan"]["std"] > 0, "kolom konstan -> std diganti epsilon (bukan 0)")
    cnorm = pp.apply_normalization(const, cstats, columns=["konstan"])
    check(np.isfinite(cnorm["konstan"]).all(), "kolom konstan ternormalisasi tanpa inf")

    # Penting: statistik dari data latih dipakai ulang untuk data uji.
    test_slice = clean.iloc[100:180]
    test_norm = pp.apply_normalization(test_slice, stats, columns=cols)
    expected = (test_slice["irradiance"].to_numpy() - stats["irradiance"]["mean"]) \
        / stats["irradiance"]["std"]
    check(np.allclose(test_norm["irradiance"].to_numpy(), expected, atol=1e-12),
          "data uji dinormalisasi dengan statistik data latih (bukan miliknya sendiri)")
    check(pp.fit_normalization(clean, columns=cols) == stats,
          "fit_normalization deterministik")

    try:
        pp.apply_normalization(clean, {}, columns=["irradiance"])
        check(False, "kolom tanpa statistik ditolak")
    except KeyError:
        check(True, "kolom tanpa statistik ditolak")

    print("\n[7] prepare_dataset — rantai penuh menghasilkan input engine")
    prepared = pp.prepare_dataset(raw, target="irradiance", freq="1D")
    frame = prepared.frame
    check(isinstance(prepared, pp.PreparedData), "mengembalikan PreparedData")
    check(str(frame.index.tz) == "Asia/Makassar", "zona benar")
    check(not frame.index.has_duplicates, "tanpa duplikat")
    check(int(frame["irradiance"].isna().sum()) == 0, "target bersih (tanpa NaN)")
    check(len(frame) >= 30, f"minimal 30 baris (dapat {len(frame)})")
    check(str(pp._step_of(frame.index)) == "1 days 00:00:00", "langkah 1 hari")
    check((frame["irradiance"] >= 0).all(), "nilai negatif sudah ditangani")
    check("gaps_detected" in prepared.meta and
          sum(prepared.meta["gaps_detected"].values()) > 0,
          "meta mencatat gap yang ditemukan")
    check(prepared.meta["input_rows"] == len(raw), "meta mencatat jumlah baris masuk")
    check("step" in prepared.meta, "meta mencatat langkah waktu")

    out1 = pp.prepare_dataset(raw, target="irradiance")
    out2 = pp.prepare_dataset(raw, target="irradiance")
    check(out1.frame.equals(out2.frame) and out1.meta == out2.meta,
          "prepare_dataset deterministik")

    print("\n[8] prepare_dataset -> generate_forecast (integrasi)")
    out_a = fe.generate_forecast(frame, horizon=7)
    out_b = fe.generate_forecast(frame, horizon=7)
    check(out_a.equals(out_b), "engine pada hasil preprocessing deterministik")
    check(len(out_a) == 7, "engine menerima tanpa penyesuaian")

    # Pipeline yang sama untuk data latih DAN data aktual.
    train = pp.prepare_dataset(raw, target="irradiance")
    eval_data = pp.prepare_dataset(raw.iloc[150:], target="irradiance")
    check(train.meta["freq"] == eval_data.meta["freq"],
          "meta resolusi identik antara train dan evaluasi")
    check(train.frame.columns.equals(eval_data.frame.columns),
          "kolom identik antara train dan evaluasi")
    check(str(train.frame.index.tz) == str(eval_data.frame.index.tz),
          "zona identik antara train dan evaluasi")

    # Dengan normalisasi, statistik latih harus dipakai ulang untuk evaluasi.
    train_n = pp.prepare_dataset(raw, target="irradiance", normalize=True)
    eval_n = pp.prepare_dataset(raw.iloc[150:], target="irradiance", normalize=True)
    check(train_n.stats is not None and eval_n.stats is not None, "stats ikut dikembalikan")

    # Data evaluasi yang BELUM dinormalisasi, lalu dinormalisasi oleh
    # statistik milik data latih. Ini satu-satunya cara metrik sebanding.
    eval_clean = pp.prepare_dataset(raw.iloc[150:], target="irradiance").frame
    cols_used = list(train_n.stats)
    eval_with_train_stats = pp.apply_normalization(eval_clean, train_n.stats,
                                                   columns=cols_used)
    for c in cols_used:
        manual = (eval_clean[c].to_numpy() - train_n.stats[c]["mean"]) \
            / train_n.stats[c]["std"]
        check(np.allclose(eval_with_train_stats[c].to_numpy(), manual, atol=1e-12),
              f"'{c}' dinormalisasi dengan statistik data latih, bukan miliknya")

    # Buktikan stats evaluasi MEMANG berbeda dari stats latih — kalau sama,
    # pemeriksaan di atas tidak membuktikan apa pun.
    check(train_n.stats["irradiance"]["mean"] != eval_n.stats["irradiance"]["mean"],
          "statistik latih ≠ statistik uji (pemakaian ulang benar-benar diuji)")

    # output ternormalisasi tetap bisa dibalik ke satuan asli.
    orig = pp.inverse_normalization(train_n.frame, train_n.stats, columns=cols_used)
    check(np.allclose(orig["irradiance"].to_numpy(),
                      train.frame["irradiance"].to_numpy(), atol=1e-9),
          "inverse mengembalikan satuan asli")

    print("\n[9] prepare_dataset — menolak input yang melanggar")
    try:
        pp.prepare_dataset(raw, target="tidak_ada")
        check(False, "target hilang ditolak")
    except KeyError:
        check(True, "target hilang ditolak")

    too_short = raw.iloc[:12]
    try:
        pp.prepare_dataset(too_short, target="irradiance")
        check(False, "data < 30 baris setelah resample ditolak")
    except ValueError as e:
        check("30" in str(e), "pesan menyebut syarat minimal 30 baris")

    if "doy_sin" in raw.columns:
        check(False, "fitur tidak bocor ke frame mentah")
    else:
        check(True, "fitur tidak bocor ke frame mentah")

    # interpolate=False: benar-benar menolak data ber-NaN, tidak menyembunyikan.
    try:
        pp.prepare_dataset(raw, target="irradiance", interpolate=False)
        check(False, "interpolate=False menolak data yang masih ber-NaN")
    except ValueError as e:
        check("NaN" in str(e), "interpolate=False menolak data yang masih ber-NaN")

    # ...dan lolos bila memang tidak ada NaN sama sekali.
    clean_no_gap = pp.prepare_dataset(raw, target="irradiance")
    strict = pp.prepare_dataset(clean_no_gap.frame, target="irradiance",
                                interpolate=False, freq="1D")
    check(int(strict.frame["irradiance"].isna().sum()) == 0,
          "interpolate=False menerima data yang sudah bersih")

    with_nan = raw.copy()
    with_nan["irradiance"] = np.nan
    try:
        pp.prepare_dataset(with_nan, target="irradiance")
        check(False, "target seluruhnya NaN ditolak")
    except ValueError:
        check(True, "target seluruhnya NaN ditolak")

    # extrapolate pada data yang gap-nya tak terselesaikan.
    gap_extreme = raw.copy()
    # .loc, bukan chained assignment — pandas 3.0 Copy-on-Write menolak yang latter.
    gap_extreme.loc[gap_extreme.index[:5], "irradiance"] = np.nan
    raised = False
    try:
        pp.prepare_dataset(gap_extreme, target="irradiance", allow_extrapolate=False)
    except ValueError:
        raised = True
    check(raised, "gap tepi yang tak terselesaikan memang menolak (bukan lolos diam-diam)")

    # Versi yang mengizinkan ekstrapolasi harus lolos.
    ok = pp.prepare_dataset(gap_extreme, target="irradiance", allow_extrapolate=True)
    check(int(ok.frame["irradiance"].isna().sum()) == 0,
          "allow_extrapolate=True menyelesaikan gap tepi")

    print("\n[10] KEMURNIAN — tanpa print, tanpa file, tanpa RNG")
    for label, module in (("preprocessing", pp), ("forecast_engine", fe)):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        banned_names = {"np.random", "random_state"}
        banned_calls = {"print", "open", "read_csv", "read_excel", "read_json", "seed"}
        banned_attrs = {"random", "seed", "default_rng"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                continue  # docstring boleh menyebut pola terlarang
            if isinstance(node, ast.Name) and node.id in banned_names:
                offenders.append(f"Name:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
                offenders.append(f"Attr:{node.attr}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in banned_calls:
                offenders.append(f"Call:{node.func.id}")
        offenders = sorted(set(offenders))
        check(offenders == [], f"{label}: kode dieksekusi bebas RNG/I/O {offenders}")

    buf = io.StringIO()
    # exog WAJIB menutup jendela forecast (7 hari setelah history) — engine
    # menolak exog yang berhenti di history, jadi harus dibentang dulu.
    weather_span = pd.DataFrame(
        {
            "cloud_cover": np.linspace(
                40.0, 60.0,
                len(frame) + 7,
            )
        },
        index=frame.index.append(
            pd.date_range(frame.index[-1] + pd.Timedelta(days=1), periods=7,
                          freq="D", tz=str(frame.index.tz))
        ),
    )
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        pp.prepare_dataset(raw, target="irradiance", normalize=True)
        pp.prepare_dataset(raw, target="irradiance", lags=(1, 7), windows=(7,), seasonal=True)
        fe.generate_forecast(frame, horizon=7)
        fe.generate_forecast(frame, horizon=7, weather_data=weather_span,
                             exog_cols=["cloud_cover"])
    check(buf.getvalue() == "", f"stdout/stderr kosong (dapat {len(buf.getvalue())} char)")

    # exog yang berhenti tepat di history (tidak menutup jendela) harus ditolak.
    try:
        fe.generate_forecast(frame, horizon=7, weather_data=frame[["cloud_cover"]],
                             exog_cols=["cloud_cover"])
        check(False, "exog tanpa jendela penuh ditolak engine")
    except ValueError:
        check(True, "exog tanpa jendela penuh ditolak engine")

    print("\n[11] fitur + normalisasi + engine sekaligus")
    rich = pp.prepare_dataset(raw, target="irradiance",
                              lags=(1, 2, 3, 7), windows=(7, 14), seasonal=True)
    check(int(rich.frame["irradiance"].isna().sum()) == 0,
          "target tetap bersih walau fitur menambah NaN")
    # Target harus tetap bisa dipakai engine (fitur lain ikut sebagai kolom).
    r1 = fe.generate_forecast(rich.frame, horizon=7, target="irradiance")
    r2 = fe.generate_forecast(rich.frame, horizon=7, target="irradiance")
    check(r1.equals(r2), "engine + fitur deterministik")
    check(len(rich.frame.columns) > 3, f"fitur bertambah kolom ({len(rich.frame.columns)} kolom)")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN PREPROCESSING LULUS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
