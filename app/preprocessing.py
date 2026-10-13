"""Preprocessing data time series — modul murni dan deterministik.

Terpisah dari :mod:`forecast_engine`. Aturan yang sama berlaku:

- Tidak membaca/menulis file apa pun.
- Tidak mencetak ke stdout/stderr.
- Tidak memakai nilai acak (tanpa ``np.random``, ``random``, ``seed``).
- Dua kali pemanggilan dengan input identik menghasilkan output identik.

Urutan langkah yang disarankan::

    coerce_timezone -> resample_regular -> interpolate_gaps
                   -> make_features     -> fit/apply_normalization
                   -> prepare_dataset   (padanan seluruh rantai)

``prepare_dataset`` adalah satu-satunya pintu yang menghasilkan frame yang
dapat langsung diterima :func:`forecast_engine.generate_forecast`. Fungsi
atom dipisah supaya masing-masing bisa diuji sendiri, dan supaya data
aktual saat evaluasi akurasi bisa melewati **rantai yang sama persis**
dengan data latih — syarat agar metrik MAE/RMSE/MAPE berarti.

Catatan implementasi pandas 3.0.1 (diverifikasi, bukan asumsi)
---------------------------------------------------------------
``Series.interpolate(method="time")`` dengan ``limit_direction="forward"`
(bawaan) hanya mengisi ke arah kanan dan **membiarkan gap terakhir tetap
NaN** — tanpa peringatan. Modul ini karena itu mengisi ujung secara
eksplisit dan menolak ekstrapolasi kecuali diminta.

``Resampler.mean()`` tidak menerima ``min_count``. Ambang minimum jumlah
observasi per bin dihitung terpisah lewat ``Resampler.count()``.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "DEFAULT_TIMEZONE",
    "PreparedData",
    "coerce_timezone",
    "resample_regular",
    "interpolate_gaps",
    "make_features",
    "fit_normalization",
    "apply_normalization",
    "inverse_normalization",
    "prepare_dataset",
]

#: Kupang berada di WITA (UTC+8).
DEFAULT_TIMEZONE = "Asia/Makassar"

#: Kolom yang nilainya tidak boleh negatif. ``temperature`` sengaja TIDAK ada
#: di sini — kontrak mengizinkan -10..50 °C.
NON_NEGATIVE_COLUMNS = frozenset({"irradiance", "ghi", "dni", "power", "energy",
                                  "ghi_wm2", "delivered_energi"})


# ---------------------------------------------------------------------------
# Bentuk keluaran pipeline
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PreparedData:
    """Hasil rantai preprocessing, lengkap dengan metadata yang bisa direproduksi.

    Attributes
    ----------
    frame
        Frame bersih, siap masuk engine (kecuali bila ``features=False``).
    stats
        Statistik normalisasi yang dipakai — ``None`` bila normalisasi
        tidak dijalankan. Wajib disimpan dan dipakai ulang untuk data aktual.
    meta
        Jejak keputusan: freq, agg, jumlah baris per tahap, dsb. Dikirim
        bersama respons API supaya hasil dapat ditelusuri.
    """

    frame: pd.DataFrame
    stats: dict[str, dict[str, float]] | None = None
    meta: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pemeriksaan dasar (dipakai ulang oleh tiap fungsi)
# ---------------------------------------------------------------------------
def _require_frame(data: pd.DataFrame, name: str = "data") -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame):
        raise TypeError(f"{name} harus pandas.DataFrame, bukan {type(data).__name__}")
    if data.empty:
        raise ValueError(f"{name} kosong")
    if not isinstance(data.index, pd.DatetimeIndex):
        raise TypeError(
            f"index {name} harus DatetimeIndex; didapat {type(data.index).__name__}"
        )
    if data.index.has_duplicates:
        dupes = data.index[data.index.duplicated()].unique()
        raise ValueError(
            f"timestamp duplikat pada {name}: {list(dupes[:5])}. "
            "Gunakan resample_regular() untuk mengagregasi, jangan menebak di sini."
        )
    if not data.index.is_monotonic_increasing:
        raise ValueError(f"index {name} harus terurut naik")
    return data


def _numeric_columns(data: pd.DataFrame, columns: Sequence[str] | None) -> list[str]:
    if columns is None:
        cols = [c for c in data.columns if pd.api.types.is_numeric_dtype(data[c])]
        if not cols:
            raise ValueError("tidak ada kolom numerik untuk diolah")
        return cols
    missing = [c for c in columns if c not in data.columns]
    if missing:
        raise KeyError(f"kolom tidak ada: {missing}; tersedia: {sorted(data.columns)}")
    non_numeric = [c for c in columns if not pd.api.types.is_numeric_dtype(data[c])]
    if non_numeric:
        raise TypeError(f"kolom non-numerik: {non_numeric}")
    return list(columns)


# ---------------------------------------------------------------------------
# 1. Zona waktu
# ---------------------------------------------------------------------------
def coerce_timezone(
    data: pd.DataFrame,
    timezone: str = DEFAULT_TIMEZONE,
) -> pd.DataFrame:
    """Samakan seluruh index ke satu zona sistem. Salinan — input tidak diubah.

    - Index tanpa tz → **ditolak**. Zona tidak boleh ditebak; menebak salah
      membuat seluruh seris meleset berjam-jam tanpa pernah gagal validasi.
      Bila sumber memang naive (cth. CSV tanpa offset), nyatakan zona aslinya
      secara eksplisit di lapisan pembaca: ``tz_localize("UTC")`` dulu, lalu
      serahkan ke fungsi ini.
    - Index ber-tz → dikonversi ke ``timezone`` (apapun zona asalnya).
    - Kolom datetime lain tidak disentuh.

    Raises
    ------
    ValueError
        Bila index tidak punya zona waktu.
    """
    frame = _require_frame(data)
    idx = frame.index

    if idx.tz is None:
        raise ValueError(
            "index data tanpa zona waktu ditolak. Berikan offset eksplisit "
            "pada timestamp (mis. +07:00 untuk WIB atau Z untuk UTC); "
            "preprocessing tidak akan menebak zona lokal."
        )
    new_idx = idx.tz_convert(timezone)

    out = frame.copy()
    out.index = new_idx
    return out


# ---------------------------------------------------------------------------
# 2. Resampling
# ---------------------------------------------------------------------------
def resample_regular(
    data: pd.DataFrame,
    *,
    freq: str = "1D",
    agg: str = "mean",
    columns: Sequence[str] | None = None,
    min_count: int | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> pd.DataFrame:
    """Rapikan ke interval tetap. **Bin kosong diisi NaN, bukan 0.**

    Memaksa interval seragam supaya ``generate_forecast`` tidak menolak
    karena ada gap. Keputusan mengisi gap ada di ``interpolate_gaps`` —
    dipisah supaya jumlah dan lokasi gap tetap terlihat saat diuji.

    Parameters
    ----------
    freq
        Interval tujuan, mis. ``"1D"``, ``"1h"``.
    agg
        Fungsi agregasi per bin: ``mean``, ``sum``, ``median``, ``last``, ``max``, ``min``.
    columns
        Kolom yang diolah. Default: semua kolom numerik.
    min_count
        Jumlah observasi minimum dalam satu bin. Bila lebih sedikit, seluruh
        bin menjadi ``NaN`` (bukan rata-rata dari 1 titik yang menyesatkan).
        Default: tanpa syarat.

    Notes
    -----
    ``min_count`` dihitung lewat ``Resampler.count()`` karena
    ``Resampler.mean()`` di pandas 3.0.1 tidak menerima parameter tersebut.
    """
    frame = _require_frame(data)
    frame = coerce_timezone(frame, timezone)

    cols = _numeric_columns(frame, columns)
    if agg not in {"mean", "sum", "median", "last", "max", "min", "first"}:
        raise ValueError(f"agg '{agg}' tidak didukung")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            resampler = frame[cols].resample(freq)
            out = getattr(resampler, agg)()
            counts = resampler.count()
        except Exception as exc:
            raise ValueError(f"gagal resample '{freq}': {exc}") from exc

    if min_count is not None:
        if min_count < 1:
            raise ValueError(f"min_count harus >= 1, diterima {min_count}")
        # Bin di bawah ambang menjadi NaN (kolom saja; index dipertahankan).
        out = out.where(counts >= min_count)

    out = out.sort_index()
    if len(out) == 0:
        raise ValueError(f"resample '{freq}' menghasilkan 0 baris")
    return out


def _step_of(index: pd.DatetimeIndex) -> pd.Timedelta:
    diffs = index.to_series().diff().dropna()
    if diffs.empty:
        raise ValueError("butuh minimal 2 baris untuk menentukan interval")
    if not (diffs == diffs.iloc[0]).all():
        raise ValueError("interval tidak seragam — jalankan resample_regular() dulu")
    return pd.Timedelta(diffs.iloc[0])


# ---------------------------------------------------------------------------
# 3. Interpolasi
# ---------------------------------------------------------------------------
def interpolate_gaps(
    data: pd.DataFrame,
    *,
    method: str = "time",
    limit: int | None = None,
    allow_extrapolate: bool = False,
    columns: Sequence[str] | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> pd.DataFrame:
    """Isi ``NaN`` di **tengah** deret; ujung hanya bila diminta.

    Parameters
    ----------
    method
        Default ``"time"`` (linier terhadap jarak waktu — deterministik dan
        tidak bergantung posisi baris, aman untuk data dengan interval tak
        seragam hasil resample).
    limit
        Panjang maksimum run ``NaN`` berurutan yang boleh diisi.
    allow_extrapolate
        ``False`` (default) → bila ada ``NaN`` di **tepi** deret (awal/akhir),
        fungsi menolak **sebelum** interpolasi berjalan, dan bila masih ada
        ``NaN`` sisa setelahnya (mis. karena ``limit``) fungsi juga menolak.
        Alasannya: pandas diam-diam mengisi ekor dengan ff-nilai-terakhir.

        ``True`` → ``NaN`` sisanya diisi dengan ``bfill`` lalu ``ffill``.
        Nilai ekstrapolasi = nilai ujung terdekat, bukan hasil model.

    Returns
    -------
    Salinan ``data`` tanpa ``NaN`` pada kolom terpilih (bila semua gap
    dapat diselesaikan).
    """
    frame = _require_frame(data)
    frame = coerce_timezone(frame, timezone)
    cols = _numeric_columns(frame, columns)

    if method not in {"time", "linear", "pad"}:
        raise ValueError(f"method '{method}' tidak didukung")

    out = frame.copy()
    subset = out[cols]

    # --- Deteksi NaN di tepi SEBELUM interpolasi -------------------------
    # pandas ``method="time"`` dengan arah default mengisi NaN ekor dengan
    # membawa nilai terakhir ke depan (ffill), tanpa peringatan. Itu kasus
    # paling berbahaya: nilai basi disuntik tepat sebelum jendela forecast,
    # sehingga forecast terlihat sah padahal bersandar pada data usang.
    # Karena itu tepi diperiksa dulu, bukan dititipkan ke perilaku pandas.
    edge_problems: dict[str, dict[str, list[str]]] = {}
    for c in cols:
        col = subset[c]
        mask = col.isna()
        if not mask.any():
            continue
        positions = mask.to_numpy().nonzero()[0]
        first_valid = (~mask).to_numpy().nonzero()[0]
        if first_valid.size == 0:
            edge_problems[c] = {"all": []}
            continue
        head = positions[positions < first_valid[0]]
        tail = positions[positions > first_valid[-1]]
        entry: dict[str, list[str]] = {}
        if head.size:
            entry["head"] = [str(t) for t in list(col.index[head[:3]])]
        if tail.size:
            entry["tail"] = [str(t) for t in list(col.index[tail[:3]])]
        if entry:
            edge_problems[c] = entry

    if edge_problems and not allow_extrapolate:
        raise ValueError(
            "NaN ditemukan di tepi deret (awal/akhir) dan tidak akan "
            "diekstrapolasi diam-diam. Mengisi ekor dengan ff-nilai-terakhir "
            "menyuntikkan data basi tepat sebelum jendela forecast. "
            f"Posisi: {edge_problems}. Lengkapi data, atau set "
            "allow_extrapolate=True bila perilaku tersebut memang dikehendaki."
        )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        filled = subset.interpolate(method=method, limit=limit)
        if allow_extrapolate:
            filled = filled.ffill().bfill()

    still = filled.isna()
    if still.any().any():
        offenders = {}
        for c in cols:
            bad = filled.index[still[c]]
            if len(bad):
                offenders[c] = [str(t) for t in list(bad[:3])]
        raise ValueError(
            "masih ada NaN setelah interpolasi — biasanya karena "
            f"``limit`` terlalu kecil. Posisi: {offenders}. "
            "Naikkan atau hapus ``limit``."
        )

    out[cols] = filled
    return out


# ---------------------------------------------------------------------------
# 4. Pembuatan fitur
# ---------------------------------------------------------------------------
def make_features(
    data: pd.DataFrame,
    *,
    lags: Sequence[int] = (1, 2, 3, 7),
    windows: Sequence[int] = (7, 14),
    target: str | None = None,
    seasonal: bool = True,
    timezone: str = DEFAULT_TIMEZONE,
) -> pd.DataFrame:
    """Tambah lag, rolling statistic, dan fitur musiman. Salinan — input tak diubah.

    Semuanya fungsi tetap dari index dan nilai lampau: tidak ada estimasi,
    tidak ada acakan, sehingga hasil identik di setiap panggilan.

    Kolom yang dihasilkan::

        lag_{n}          nilai target t-n
        roll_mean_{w}    rata-rata bergerak w baris terakhir (termasuk baris kini)
        roll_std_{w}     simpangan baku bergerak w baris
        doy_sin/doy_cos  posisi dalam tahun (siklus musiman)

    ``roll_*`` memakai ``min_periods=w`` sehingga baris awal ``NaN`` — wajar
    dan sengaja; ``interpolate_gaps`` tidak boleh dipakai untuk mengisinya
    karena nilai awal tidak punya masa lalu.
    """
    frame = _require_frame(data)
    frame = coerce_timezone(frame, timezone)
    cols = _numeric_columns(frame, None)

    if target is not None and target not in frame.columns:
        raise KeyError(f"target '{target}' tidak ada; tersedia: {sorted(frame.columns)}")
    source = target if target is not None else cols[0]

    if any(int(w) < 2 for w in windows):
        raise ValueError("semua window harus >= 2")
    if any(int(l) < 1 for l in lags):
        raise ValueError("semua lag harus >= 1")

    out = frame.copy()
    base = out[source].astype("float64")

    for lag in lags:
        n = int(lag)
        out[f"lag_{n}"] = base.shift(n)

    for window in windows:
        w = int(window)
        out[f"roll_mean_{w}"] = base.rolling(window=w, min_periods=w).mean()
        out[f"roll_std_{w}"] = base.rolling(window=w, min_periods=w).std(ddof=0)

    if seasonal:
        doy = out.index.dayofyear.to_numpy(dtype="float64")
        # 365.25 -> periode tahunan stabil lintas tahun kabisat; deterministik.
        angle = 2.0 * np.pi * doy / 365.25
        out["doy_sin"] = np.sin(angle)
        out["doy_cos"] = np.cos(angle)

    return out


# ---------------------------------------------------------------------------
# 5. Normalisasi
# ---------------------------------------------------------------------------
def fit_normalization(
    data: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    epsilon: float = 1e-9,
) -> dict[str, dict[str, float]]:
    """Hitung statistik normalisasi dari **data latih saja**.

    Std memakai ``ddof=0`` (populasi) — eksak dan tidak berubah bila
    sebagian baris ditambah/dihapus. Bila ``std < epsilon`` kolom dianggap
    konstan: ``std`` dikunci ke ``epsilon`` supaya pembagian tidak menghasilkan
    ``inf``.

    Fungsi ini terpisah dari ``apply_normalization`` supaya statistik bisa
    disimpan sebagai bagian dari artefak model dan **dipakai ulang** untuk
    data aktual — normalisasi ulang dengan statistik data uji akan membuat
    metrik evaluasi tidak sebanding.
    """
    frame = _require_frame(data)
    cols = _numeric_columns(frame, columns)

    stats: dict[str, dict[str, float]] = {}
    for c in cols:
        series = frame[c].astype("float64")
        observed = series.dropna()
        if observed.empty:
            raise ValueError(f"kolom '{c}' seluruhnya NaN — tidak bisa menghitung statistik")
        mean = float(observed.mean())
        std = float(observed.std(ddof=0))
        stats[c] = {
            "mean": mean,
            "std": std if std >= epsilon else float(epsilon),
            "min": float(observed.min()),
            "max": float(observed.max()),
            "count": float(observed.size),
        }
    return stats


def apply_normalization(
    data: pd.DataFrame,
    stats: Mapping[str, Mapping[str, float]],
    *,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Terapkan ``stats`` hasil :func:`fit_normalization`.

    Kolom yang tidak punya statistik dibiarkan apa adanya. Kolom yang
    diminta lewat ``columns`` tapi tidak ada di ``stats`` → ditolak.
    """
    frame = _require_frame(data)
    cols = _numeric_columns(frame, columns) if columns is not None else list(frame.columns)

    missing = [c for c in cols if c not in stats]
    if missing and columns is not None:
        raise KeyError(f"stats tidak punya kolom {missing}")

    out = frame.copy()
    for c in cols:
        if c not in stats:
            continue
        mean = float(stats[c]["mean"])
        std = float(stats[c]["std"])
        if not np.isfinite(std) or std == 0:
            raise ValueError(f"std tidak valid untuk kolom '{c}': {std}")
        out[c] = (out[c].astype("float64") - mean) / std
    return out


def inverse_normalization(
    data: pd.DataFrame,
    stats: Mapping[str, Mapping[str, float]],
    *,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Balikkan normalisasi (z → satuan asli). Kebalikan persis dari apply."""
    frame = _require_frame(data)
    cols = _numeric_columns(frame, columns) if columns is not None else list(frame.columns)

    out = frame.copy()
    for c in cols:
        if c not in stats:
            continue
        mean = float(stats[c]["mean"])
        std = float(stats[c]["std"])
        out[c] = out[c].astype("float64") * std + mean
    return out


# ---------------------------------------------------------------------------
# 6. Pipeline penuh
# ---------------------------------------------------------------------------
def prepare_dataset(
    data: pd.DataFrame,
    *,
    target: str,
    freq: str = "1D",
    agg: str = "mean",
    min_count: int | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    interpolate: bool = True,
    allow_extrapolate: bool = False,
    lags: Sequence[int] = (),
    windows: Sequence[int] = (),
    seasonal: bool = False,
    normalize: bool = False,
    clip_negative: bool = True,
    columns: Sequence[str] | None = None,
) -> PreparedData:
    """Jalankan seluruh rantai preprocessing dan kembalikan ``PreparedData``.

    Urutan::

        coerce_timezone -> resample_regular -> interpolate_gaps
                       -> (opsional) make_features
                       -> (opsional) fit_normalization
                       -> validasi akhir

    Rantai ini **harus dipakai sama** untuk data latih dan data aktual
    saat evaluasi akurasi; ``meta`` mencatat setiap keputusan supaya
    perbedaan hasil dapat ditelusuri.

    Parameters
    ----------
    target
        Kolom target. Wajib ada dan wajib bersih (tanpa NaN) setelah rantai.
    clip_negative
        ``True`` (default) → nilai negatif pada kolom fisik-wajib-positif
        ditandai ``NaN`` lalu ikut diinterpolasi. ``False`` → nilai negatif
        dibiarkan dan akan **ditolak** oleh engine.

    Returns
    -------
    PreparedData
        ``frame`` siap dikirim ke ``generate_forecast(target=target)``.
    """
    if target not in data.columns:
        raise KeyError(f"target '{target}' tidak ada; tersedia: {sorted(data.columns)}")

    meta: dict[str, object] = {
        "freq": freq,
        "agg": agg,
        "min_count": min_count,
        "timezone": timezone,
        "target": target,
        "input_rows": int(len(data)),
    }

    work = coerce_timezone(data, timezone)

    # (a) numerik dan kolom yang dibutuhkan
    cols = _numeric_columns(work, columns)
    if target not in cols:
        cols = cols + [target]
        work = work.copy()
        work[target] = work[target].astype("float64")

    # (b) nilai negatif pada kolom yang wajib >= 0
    if clip_negative:
        bad = {}
        for c in cols:
            if c in NON_NEGATIVE_COLUMNS and (work[c] < 0).any():
                bad[c] = int((work[c] < 0).sum())
        if bad:
            work = work.copy()
            for c in bad:
                work.loc[work[c] < 0, c] = np.nan
        meta["clipped_negative"] = bad

    # (c) resample -> ratakan interval
    before = len(work)
    work = resample_regular(work, freq=freq, agg=agg, columns=cols,
                            min_count=min_count, timezone=timezone)
    meta["before_resample_rows"] = before
    meta["after_resample_rows"] = int(len(work))

    # (d) hitung gap sebelum diisi — penting untuk laporan kualitas data
    gaps = {c: int(work[c].isna().sum()) for c in cols}
    meta["gaps_detected"] = gaps

    # (e) interpolasi
    if interpolate:
        # Kolom rolling/lag sengaja TIDAK dikirim ke sini: mereka belum ada
        # dan memang harus dibiarkan NaN pada baris awal.
        work = interpolate_gaps(work, columns=cols, allow_extrapolate=allow_extrapolate,
                                timezone=timezone)
    else:
        still = int(work[cols].isna().sum().sum())
        if still:
            raise ValueError(
                f"interpolate=False tapi masih ada {still} NaN — "
                "engine menolak data ber-NaN"
            )

    # (f) fitur opsional (membuat NaN baru secara sengaja pada baris awal)
    if lags or windows or seasonal:
        work = make_features(work, lags=lags, windows=windows, target=target,
                             seasonal=seasonal, timezone=timezone)
        meta["features"] = {
            "lags": [int(x) for x in lags],
            "windows": [int(x) for x in windows],
            "seasonal": bool(seasonal),
        }

    # (g) validasi akhir pada target — inilah yang disyaratkan engine
    series = work[target].astype("float64")
    if series.isna().any():
        hole = list(series.index[series.isna()][:5])
        raise ValueError(
            f"target '{target}' masih punya NaN setelah tahap bersih: "
            f"{[str(t) for t in hole]}. Perbesar allow_extrapolate/limit, "
            "atau periksa rentang data."
        )
    if len(work) < 30:
        raise ValueError(
            f"hasil hanya {len(work)} baris; engine minimal 30. "
            f"Input {before} baris dengan freq '{freq}' tidak cukup."
        )

    step = _step_of(work.index)
    meta["step"] = str(step)

    # (h) normalisasi opsional
    stats = None
    if normalize:
        stats = fit_normalization(work, columns=cols)
        work = apply_normalization(work, stats, columns=cols)
        meta["normalized_columns"] = cols

    if not work.index.is_monotonic_increasing:
        work = work.sort_index()

    return PreparedData(frame=work, stats=stats, meta=meta)
