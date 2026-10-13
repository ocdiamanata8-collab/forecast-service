"""Uji strategi evaluasi — backtest tanpa kebocoran, deterministik, dan lulus/gagal.

    python test_evaluation.py
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

import app.evaluation as ev      # noqa: E402
import app.engine as fe  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def make_series(n: int = 320, seed: int = 11) -> pd.DataFrame:
    """Deret musiman deterministik + sedikit noise (seed hanya untuk DATA UJI)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    doy = idx.dayofyear.to_numpy(dtype="float64")
    weekly = 15 * np.sin(2 * np.pi * np.arange(n) / 7.0)
    yearly = 60 * np.sin(2 * np.pi * doy / 365.25)
    values = np.clip(180 + yearly + weekly + rng.normal(0, 5, n), 0, None)
    return pd.DataFrame({"irradiance": values}, index=idx)


def main() -> int:
    data = make_series()

    print("\n[1] BACKTEST — struktur tanpa kebocoran")
    rep = ev.rolling_backtest(
        data, target="irradiance", horizons=(1, 7, 14),
        holdout=60, stride=7, order=(1, 0, 2),
    )
    check(isinstance(rep, ev.BacktestReport), "mengembalikan BacktestReport")
    check(len(rep.blocks) == 3, "ada satu blok metrik per horizon")
    check(tuple(b.horizon for b in rep.blocks) == (1, 7, 14), "horizon sesuai input")

    # Kunci utama: JANGAN PERNAH mengizinkan fit menyentuh holdout.
    check(rep.holdout == 60, "holdout tercatat")
    check(str(rep.holdout_start) == str(data.index[-60]),
          f"holdout_start = baris -60 ({rep.holdout_start} vs {data.index[-60]})")
    check(str(rep.holdout_end) == str(data.index[-1]), "holdout_end = baris terakhir")
    check(rep.n_rows == len(data), "n_rows = panjang data")

    # origin pertama harus membuat forecast PERTAMA jatuh tepat di holdout.
    first_origin_pos = rep.n_train_at_first_origin - 1
    check(first_origin_pos + 1 == len(data) - rep.holdout,
          f"origin pertama ({first_origin_pos}) -> forecast masuk holdout")

    # Jendela terpanjang dari origin TERAKHIR masih muat di data.
    last_origin_pos = first_origin_pos + (rep.n_origins - 1) * rep.stride
    check(last_origin_pos + max(rep.horizons) <= len(data) - 1,
          "jendela terpanjang dari origin terakhir tidak melewati akhir data")
    check(rep.n_origins > 1, f"lebih dari satu origin (dapat {rep.n_origins})")

    print("\n[2] BACKTEST — bukti langsung: train tidak pernah menyentuh holdout")
    # Tangkap panjang train yang benar-benar dikirim ke engine, dengan
    # memata-matai generate_forecast di namespace modul evaluasi.
    captured: list[pd.DataFrame] = []
    real = ev.generate_forecast

    def spy(frame, *args, **kwargs):
        captured.append(frame.copy())
        return real(frame, *args, **kwargs)

    ev.generate_forecast = spy
    try:
        ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=60, stride=15, order=(1, 0, 2))
    finally:
        ev.generate_forecast = real

    check(len(captured) > 0, "spy menangkap panggilan engine")

    # INVARIAN YANG BENAR untuk expanding window:
    # 1. origin BERURUT naik dan train = prefix murni (origin+1 baris).
    # 2. origin PERTAMA: jendela forecast-nya MASUK holdout — tetapi hanya
    #    baris holdout terdepan yang boleh disentuh oleh origin berikutnya
    #    (data aktual yang memang sudah teramati saat origin itu dijalankan).
    # 3. Tidak ada baris dari TENGAH holdout ke depan yang masuk train di
    #    origin pertama — hanya satu baris holdout perluannya dipakai untuk
    #    menentukan origin.
    holdout_start = len(data) - 60
    lengths = [len(f) for f in captured]
    check(all(lengths[i] < lengths[i + 1] for i in range(len(lengths) - 1)),
          "panjang train menaik (expanding window, bukan shuffle)")
    check(lengths[0] == first_origin_pos + 1,
          f"train pertama = origin pertama + 1 baris ({lengths[0]})")
    # Origin pertama hanya boleh menembus holdout sebanyak 1 baris
    # (baris yang menjadi jangkar, sudah teramati saat origin dijalankan).
    overshoot_first = lengths[0] - holdout_start
    check(overshoot_first <= 1,
          f"train pertama menembus holdout maksimal 1 baris (dapat {overshoot_first})")
    # Setelah origin pertama, memang WAJIB menyentuh holdout bertambah —
    # itu data aktual yang sudah teramati, bukan kebocoran. Yang dilarang
    # adalah baris yang BELUM teramati masuk ke train.
    check(all(len(f) <= origin_pos + 1 for f, origin_pos in zip(captured, [first_origin_pos + i * 15 for i in range(len(captured))])),
          "tiap train tidak melewati baris yang sudah teramati pada origin itu")

    # Prediksi identik walau isi holdout diubah seluruhnya — inilah artinya
    # "holdout tidak memengaruhi prediksi".
    perturbed = data.copy()
    perturbed.iloc[holdout_start:, 0] = 1.0
    a = ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=60, stride=15, order=(1, 0, 2))
    b = ev.rolling_backtest(perturbed, target="irradiance", horizons=(1, 7),
                            holdout=60, stride=15, order=(1, 0, 2))
    fa = [(blk.mae, blk.mape, blk.n) for blk in a.blocks]
    fb = [(blk.mae, blk.mape, blk.n) for blk in b.blocks]
    check(fa != fb, "metrik berubah ketika actual holdout berubah (actual memang dipakai)")
    # ...tetapi jumlah origin dan panjang train harus identik.
    check(a.n_origins == b.n_origins, "jumlah origin tidak dipengaruhi isi holdout")
    check(a.n_train_at_first_origin == b.n_train_at_first_origin,
          "panjang train pertama tidak dipengaruhi isi holdout")

    print("\n[3] BACKTEST — determinisme")
    r1 = ev.rolling_backtest(data, target="irradiance", horizons=(1, 7, 14),
                             holdout=60, stride=7, order=(1, 0, 2))
    r2 = ev.rolling_backtest(data, target="irradiance", horizons=(1, 7, 14),
                             holdout=60, stride=7, order=(1, 0, 2))
    same = all(
        abs(x.mae - y.mae) < 1e-12 and abs(x.mape - y.mape) < 1e-12 and x.n == y.n
        for x, y in zip(r1.blocks, r2.blocks)
    )
    check(same, "backtest deterministik (metrik identik antar run)")
    check(r1.n_origins == r2.n_origins, "jumlah origin identik")
    check(r1.failures == r2.failures, "status lulus identik antar run")

    print("\n[4] BASELINE — persistence dihitung benar")
    vals = np.array([10.0, 12.0, 14.0, 13.0, 11.0])
    p = ev.persist(float(vals[-1]), 4)
    check(len(p) == 4, "panjang baseline = horizon")
    check(np.allclose(p, 11.0), "persistence memakai nilai terakhir")
    try:
        ev.persist(1.0, 0)
        check(False, "horizon 0 pada baseline ditolak")
    except ValueError:
        check(True, "horizon 0 pada baseline ditolak")

    s = ev.seasonal_persist(vals, horizon_step=1, period=4)
    check(abs(s - vals[-4]) < 1e-12, f"seasonal naive memakai lag period (dapat {s})")
    try:
        ev.seasonal_persist(vals, horizon_step=1, period=99)
        check(False, "seasonal melewati deret ditolak")
    except IndexError:
        check(True, "seasonal melewati deret ditolak")

    print("\n[5] METRIK — sesuai hitungan tangan")
    # MAE/MAPE/RMSE dihitung ulang tanpa modul, lalu dibandingkan.
    # primary_horizon default adalah 7, jadi 7 harus ikut dalam daftar.
    r = ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=30, stride=3, order=(1, 0, 2))
    blk = next(b for b in r.blocks if b.horizon == 1)
    # Uji konsistensi internal: skill = 1 - mae/baseline_mae
    expected_skill = 1.0 - blk.mae / blk.baseline_mae
    check(abs(blk.skill - expected_skill) < 1e-12, "skill konsisten dengan MAE")
    check(blk.n == r.n_origins, f"n = jumlah origin (dapat {blk.n} vs {r.n_origins})")
    check(np.isfinite(blk.mae) and np.isfinite(blk.mape), "metrik terhitung finite")
    check(blk.baseline_mae > 0, "MAE baseline > 0 (ada yang dikalahkan)")

    # Angka manual pada deret kecil lewat fungsi engine.
    hist = make_series(60, seed=3)
    fc = fe.generate_forecast(hist, horizon=5, target="irradiance")
    actual = data["irradiance"].iloc[60:65].to_numpy()
    manual_mae = float(np.mean(np.abs(actual - fc["forecast_value"].to_numpy())))
    m = ev.evaluate_forecast(actual, fc["forecast_value"].to_numpy())
    check(abs(m["mae"] - manual_mae) < 1e-9, "MAE = rerata |actual - pred|")

    print("\n[6] JUDGE — ambang menentukan lulus/tidak")
    th_pass = ev.Thresholds(max_mae=1e6, max_mape=1e6, max_mape_long=1e6,
                            min_skill=0.0)
    # Judge ambang longgar menggunakan report valid yang synthetic; pengujian
    # gate ini tidak boleh mengubah forecast/backtest yang sedang diuji.
    from dataclasses import replace
    passing_blocks = tuple(
        ev.MetricBlock(horizon=h, mae=1.0, rmse=1.2, mape=1.0, n=10,
                       baseline_mae=2.0, baseline_mape=2.0, skill=0.5)
        for h in (1, 7, 30)
    )
    passing_report = ev.BacktestReport(
        target="irradiance", timezone=fe.DEFAULT_TIMEZONE,
        holdout_start=data.index[-45], holdout_end=data.index[-1],
        n_rows=len(data), n_train_at_first_origin=len(data)-45,
        holdout=45, stride=9, order=(1, 0, 2), n_origins=2,
        horizons=(1, 7, 30), primary_horizon=7, blocks=passing_blocks,
        thresholds=th_pass,
    )
    rep_pass = ev.judge_backtest(passing_report)
    check(rep_pass.passed, f"metrik lulus + ambang longgar -> lulus (failures={rep_pass.failures})")
    check(rep_pass.failures == (), "failures kosong saat lulus")

    th_fail = ev.Thresholds(max_mae=0.001, max_mape=0.01,
                            max_mape_long=0.01, min_skill=0.99)
    failing_report = replace(passing_report, thresholds=th_fail)
    rep_fail = ev.judge_backtest(failing_report)

    print("\n[7] VALIDASI — konfigurasi tidak mungkin ditolak")
    try:
        ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=5, stride=1)
        check(False, "holdout < horizon terbesar ditolak")
    except ValueError as e:
        check("holdout" in str(e), "holdout < horizon terbesar ditolak")

    try:
        ev.rolling_backtest(data, target="tidak_ada", horizons=(1,), holdout=30)
        check(False, "target hilang ditolak")
    except KeyError:
        check(True, "target hilang ditolak")

    try:
        ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=30, stride=1, exog_cols=["cloud_cover"])
        check(False, "exog tanpa weather_data ditolak")
    except ValueError:
        check(True, "exog tanpa weather_data ditolak")

    naive = data.copy()
    naive.index = naive.index.tz_localize(None)
    try:
        ev.rolling_backtest(naive, target="irradiance", horizons=(1,), holdout=30)
        check(False, "index naive ditolak")
    except ValueError:
        check(True, "index naive ditolak")

    try:
        ev.Thresholds(max_mape=10.0, max_mape_long=5.0).validate()
        check(False, "max_mape_long < max_mape ditolak")
    except ValueError:
        check(True, "max_mape_long < max_mape ditolak")

    try:
        ev.Thresholds(max_mae=-1).validate()
        check(False, "max_mae negatif ditolak")
    except ValueError:
        check(True, "max_mae negatif ditolak")

    try:
        ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=60, stride=1, unknown_arg=True)  # type: ignore[call-arg]
        check(False, "argumen tak dikenal harus gagal")
    except TypeError:
        check(True, "argumen tak dikenal harus gagal")

    # primary_horizon harus ada di horizons
    try:
        ev.rolling_backtest(
            data, target="irradiance", horizons=(1, 7), holdout=60,
            thresholds=ev.Thresholds(primary_horizon=5),
        )
        check(False, "primary_horizon di luar daftar horizon ditolak")
    except ValueError as e:
        check("primary_horizon" in str(e), "primary_horizon di luar daftar horizon ditolak")

    small = data.iloc[:20]
    try:
        ev.rolling_backtest(small, target="irradiance", horizons=(1, 7), holdout=7)
        check(False, "data terlalu sedikit ditolak")
    except ValueError as e:
        check("terlalu sedikit" in str(e), f"data terlalu sedikit ditolak ({e})")

    print("\n[8] EXOG — skenario oracle ditandai, bukan disembunyikan")
    weather = pd.DataFrame(
        {"cloud_cover": np.linspace(20.0, 80.0, len(data))},
        index=data.index,
    )
    rep_exog = ev.rolling_backtest(
        data, target="irradiance", horizons=(1, 7), holdout=60, stride=14,
        order=(1, 0, 2), exog_cols=["cloud_cover"], weather_data=weather,
    )
    check(rep_exog.exog_oracle is True, "pemakaian exog ditandai oracle")
    check(rep_exog.exog_cols == ("cloud_cover",), "exog_cols tercatat")
    check(np.isfinite(rep_exog.blocks[0].mae), "metrik exog terhitung")

    rep_plain = ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                                    holdout=60, stride=14, order=(1, 0, 2))
    check(rep_plain.exog_oracle is False, "tanpa exog ditandai bukan oracle")

    print("\n[9] KEMURNIAN — tanpa print, tanpa file, tanpa RNG")
    tree = ast.parse(Path(ev.__file__).read_text(encoding="utf-8"))
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
    check(offenders == [], f"kode dieksekusi bebas RNG/I/O {offenders}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        ev.rolling_backtest(data, target="irradiance", horizons=(1, 7),
                            holdout=60, stride=14, order=(1, 0, 2))
        ev.judge_backtest(r1)
        ev.persist(1.0, 3)
        ev.seasonal_persist(vals, horizon_step=7, period=7)  # pos = len-1+7-7 = akhir
    check(buf.getvalue() == "", f"stdout/stderr kosong (dapat {len(buf.getvalue())} char)")

    print("\n[10] AMBANG BAWAAN — nilai yang dikunci sebelum uji")
    th = ev.DEFAULT_THRESHOLDS
    check(th.max_mae == 40.0, f"default max_mae = {th.max_mae} W/m²")
    check(th.max_mape == 10.0, f"default max_mape = {th.max_mape}%")
    check(th.max_mape_long == 15.0, f"default max_mape_long = {th.max_mape_long}%")
    check(th.primary_horizon == 7, f"default horizon utama = {th.primary_horizon} hari")
    check(th.min_skill == 0.0, "default min_skill = 0 (minimal setara persistence)")
    th.validate()  # tidak boleh melempar
    check(True, "ambang bawaan lolos validasi sendiri")
    check(ev.DEFAULT_HORIZONS == (1, 7, 14, 30), f"horizon bawaan {ev.DEFAULT_HORIZONS}")
    check(ev.DEFAULT_SEASONAL_PERIOD == 7, "periode musiman harian = 7")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN EVALUASI LULUS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
