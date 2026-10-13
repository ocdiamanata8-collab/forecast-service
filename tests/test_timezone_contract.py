"""Uji kontrak zona waktu tunggal — pencegah bug integrasi paling mahal.

    python test_timezone_contract.py

Skenario yang dibuktikan:

  1. Zona keluar SELALU ``Asia/Makassar`` dengan offset eksplisit ``+08:00``.
  2. Zona masukan boleh berbeda-beda (SCADA UTC, inverter WIB, cuaca WIT)
     — dikonversi, bukan ditolak.
  3. Timestamp TANPA zona selalu ditolak; zona tidak pernah ditebak.
  4. Momen fisik yang sama dari zona berbeda -> output identik.
     (Inilah yang gagal bila history UTC dipadukan exog WIB tanpa konversi.)
  5. Zona engine == zona preprocessing == zona kontrak API.
"""

from __future__ import annotations

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


def make_history(n: int = 120, tz: str | None = "Asia/Makassar") -> pd.DataFrame:
    """Bangun history pada satu zona. ``tz=None`` menghasilkan index naive."""
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz=tz)
    doy = idx.dayofyear.to_numpy(dtype="float64")
    rng = np.random.default_rng(42)  # hanya untuk MEMBUAT data uji, bukan model
    values = np.clip(180 + 60 * np.sin(2 * np.pi * doy / 365.25)
                     + rng.normal(0, 8, n), 0, None)
    return pd.DataFrame({"irradiance": values}, index=idx)


def main() -> int:
    print("\n[1] ZONA KELUAR — offset eksplisit, selalu Asia/Makassar")
    base = make_history()
    out = fe.generate_forecast(base, horizon=5)

    check(str(out["forecast_time"].dt.tz) == fe.DEFAULT_TIMEZONE,
          "forecast_time berada di zona sistem")
    offsets = {int(ts.utcoffset().total_seconds()) for ts in out["forecast_time"]}
    check(offsets == {8 * 3600}, f"offset keluar eksplisit +08:00 (dapat {offsets})")

    # Format serialisasi kontrak: harus menampilkan +08:00, bukan Z / tanpa offset.
    iso = out["forecast_time"].iloc[0].isoformat()
    check(iso.endswith("+08:00"), f"format ISO membawa +08:00 (dapat '{iso}')")
    check("+07:00" not in iso, "tidak membawa offset +07:00 (bukan zona keliru)")

    print("\n[2] ZONA MASUKAN BOLEH BERBEDA — dikonversi, bukan ditolak")
    # Sumber nyata: history sudah WITA, exog cuaca disimpan dalam UTC (kias ERA5).
    # Penting: rentangnya dibentuk dari MOMEN yang sama (lalu dikonversi ke UTC
    # untuk disimpan), bukan dari jam-kalender UTC. Momen 00:00 WITA == 16:00
    # UTC hari sebelumnya — kalau dibangun dari "tengah malam UTC", exog akan
    # bergeser 8 jam dan gagal selaras (itu masalah alignment, bukan zona).
    history = make_history(tz="Asia/Makassar")
    horizon = 5
    wita_moment = pd.date_range(
        history.index[0] - pd.Timedelta(days=4),
        periods=4 + len(history) + horizon,
        freq="D",
    )
    weather_utc = pd.DataFrame(
        {"cloud_cover": np.linspace(20.0, 70.0, len(wita_moment))},
        index=wita_moment.tz_convert("UTC"),
    )
    check(str(weather_utc.index.tz) == "UTC", "exog uji memang ber-zona UTC")
    try:
        fe.generate_forecast(history, weather_data=weather_utc, horizon=5,
                             exog_cols=["cloud_cover"])
        check(True, "exog ber-zona UTC diterima (dikonversi, bukan ditolak)")
    except ValueError as e:
        check(False, f"exog UTC ditolak — zona harusnya dikonversi: {e}")

    # Versi yang sudah dikonversi manual harus memberi hasil sama persis.
    weather_wita = weather_utc.tz_convert("Asia/Makassar")
    a = fe.generate_forecast(history, weather_data=weather_utc, horizon=5,
                             exog_cols=["cloud_cover"])
    b = fe.generate_forecast(history, weather_data=weather_wita, horizon=5,
                             exog_cols=["cloud_cover"])
    check(a.equals(b),
          "exog UTC == exog WITA setelah konversi (momen sama -> hasil sama)")

    # Exog dalam zona berbeda TIDAK boleh selisih hasil dari exog zona sistem.
    check(str(a["forecast_time"].dt.tz) == fe.DEFAULT_TIMEZONE,
          "keluaran tetap memakai zona sistem walau input beragam")

    print("\n[3] TIMESTAMP TANPA ZONA — selalu ditolak, tidak pernah ditebak")
    naive = make_history(tz=None)
    try:
        fe.generate_forecast(naive, horizon=5)
        check(False, "index naive ditolak")
    except ValueError as e:
        check("tanpa zona" in str(e) or "offset" in str(e),
              "index naive ditolak dengan pesan jelas")

    naive_weather = weather_utc.tz_localize(None)
    try:
        fe.generate_forecast(history, weather_data=naive_weather, horizon=5,
                             exog_cols=["cloud_cover"])
        check(False, "exog naive ditolak")
    except ValueError:
        check(True, "exog naive ditolak")

    print("\n[4] MOMEN SAMA DARI ZONA BERBEDA -> OUTPUT IDENTIK")
    # Fisik yang sama: 2025-01-01 00:00 WITA == 2024-12-31 16:00 UTC.
    h_wita = make_history(tz="Asia/Makassar")
    h_utc = h_wita.tz_convert("UTC")
    h_jakarta = h_wita.tz_convert("Asia/Jakarta")

    out_wita = fe.generate_forecast(h_wita, horizon=7)
    out_utc = fe.generate_forecast(h_utc, horizon=7)
    out_jkt = fe.generate_forecast(h_jakarta, horizon=7)

    check(out_wita.equals(out_utc),
          "history WITA == history UTC (zona input tidak memengaruhi angka)")
    check(out_wita.equals(out_jkt),
          "history WITA == history WIB (konversi bolak-balik identik)")
    check(str(out_utc["forecast_time"].dt.tz) == fe.DEFAULT_TIMEZONE,
          "zona keluar dikembalikan ke Asia/Makassar, bukan mengikuti input")

    # Nilai forecast identik meski representasi timestamp input berbeda.
    check(np.array_equal(out_wita["forecast_value"].to_numpy(),
                         out_utc["forecast_value"].to_numpy()),
          "nilai forecast bitwise identik lintas zona input")

    print("\n[5] PREPROCESSING — konversi sebelum engine, hasil sama")
    # Data mentah UTC + data mentah WITA = momen sama.
    raw_utc = pd.DataFrame(
        {
            "irradiance": h_wita["irradiance"].to_numpy(),
            "temperature": np.full(len(h_wita), 27.0),
        },
        index=h_wita.index.tz_convert("UTC"),
    )
    prep_utc = pp.prepare_dataset(raw_utc, target="irradiance", freq="1D")
    prep_wita = pp.prepare_dataset(h_wita, target="irradiance", freq="1D")

    check(str(prep_utc.frame.index.tz) == fe.DEFAULT_TIMEZONE,
          "preprocessing mengembalikan zona sistem")
    check(prep_utc.frame["irradiance"].equals(prep_wita.frame["irradiance"]),
          "data UTC dan WITA menghasilkan deret identik setelah prep")
    check(prep_utc.frame.index.equals(prep_wita.frame.index),
          "index identik setelah konversi")

    # Naive di preprocessing juga harus ditolak — zona tidak boleh ditebak.
    naive_prep = h_wita.copy()
    naive_prep.index = naive_prep.index.tz_localize(None)
    try:
        pp.coerce_timezone(naive_prep)
        check(False, "preprocessing menolak index naive")
    except ValueError:
        check(True, "preprocessing menolak index naive")
    check(naive_prep.index.tz is None, "frame asli tidak dimutasi")

    # Namun engine tetap menolak naive — konversi ada di preprocessing,
    # bukan diserahkan ke keberuntungan.
    try:
        fe.generate_forecast(naive_prep, horizon=5)
        check(False, "engine menolak index naive dari preprocessing")
    except ValueError:
        check(True, "engine menolak index naive dari preprocessing")

    print("\n[6] KESAMAAN ZONA ANTAR-MODUL DAN KONTRAK")
    check(fe.DEFAULT_TIMEZONE == pp.DEFAULT_TIMEZONE,
          f"engine dan preprocessing memakai zona sama ({fe.DEFAULT_TIMEZONE})")
    check(fe.DEFAULT_TIMEZONE == "Asia/Makassar", "zona sistem = Asia/Makassar")
    check("+07:00" not in fe.DEFAULT_TIMEZONE and "Jakarta" not in fe.DEFAULT_TIMEZONE,
          "bukan Asia/Jakarta (+07:00) — PLTS Kupang adalah WITA (+08:00)")

    # Kontrak API wajib menyebut zona dan offset yang sama (ada di root repo).
    contract = Path(__file__).resolve().parents[1] / "KONTRAK_API_v1.md"
    if contract.exists():
        text = contract.read_text(encoding="utf-8")
        check("Asia/Makassar" in text, "kontrak API menyebut Asia/Makassar")
        check("+08:00" in text, "kontrak API memakai offset +08:00")
        check("naive" in text.lower() or "tanpa zona" in text.lower()
              or "wajib tz-aware" in text, "kontrak API melarang timestamp tanpa zona")
    else:
        check(False, "KONTRAK_API_v1.md tidak ditemukan")

    print("\n[7] METAMORFOSE TIMESTAMP — konversi tidak menggeser momen fisik")
    ts = pd.Timestamp("2025-06-15T10:30:00", tz="Asia/Jakarta")
    same_moment = ts.tz_convert("Asia/Makassar")
    check(ts.value == same_moment.value,
          "konversi WIB->WITA mempertahankan instan UTC yang sama")
    check(int(same_moment.utcoffset().total_seconds()) == 8 * 3600, "offset berubah jadi +08:00")
    check(same_moment.hour == 11, f"jam lokal jadi 11 (dapat {same_moment.hour})")

    # Deret history UTC yang dikonversi harus tetap urut dan seragam.
    utc_series = pd.date_range("2025-01-01", periods=50, freq="D", tz="UTC") \
        .tz_convert("Asia/Makassar")
    check(utc_series.is_monotonic_increasing, "konversi menjaga urutan menaik")
    check((utc_series.to_series().diff().dropna() == pd.Timedelta(days=1)).all(),
          "konversi menjaga langkah 1 hari (tanpa jam-parsial)")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SEMUA PEMERIKSAAN ZONA WAKTU LULUS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
