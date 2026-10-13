"""Mesin forecasting irradiance energi — modul murni dan deterministik.

Kontrak modul ini:

- Tidak membaca/menulis file apa pun (tidak ada ``open``, ``read_csv``,
  ``xr.open_dataset``).
- Tidak mencetak ke stdout/stderr (tidak ada ``print``).
- Tidak menggunakan nilai acak (tidak ada ``np.random``, ``random``,
  ``random_state`` yang tidak ditentukan).
- Dua kali pemanggilan dengan input identik menghasilkan output identik
  secara bitwise — syarat supaya API bisa di-test otomatis.

Pemanggilan I/O (membaca CSV, mencetak log) menjadi tanggung jawab lapisan
di atasnya: ``app.py`` untuk API, script evaluasi untuk laporan.

Urutan ARIMA dikunci sebagai konstanta, BUKAN hasil grid search di dalam
fungsi. Grid search pada tiap panggilan API membuat latensi tidak terprediksi
dan hasilnya bergantung pada data masuk, sehingga versi model tidak stabil.
Urutan diambil dari hasil tervalidasi pada notebook harian:

    WIB  ARIMA(3,0,0)  AIC 3606.86  MAPE 6.49%
    WITA ARIMA(1,0,2)  AIC 3586.65  MAPE 6.44%   <- Kupang (WITA)
    WIT  ARIMA(2,0,1)  AIC 3674.28  MAPE 7.50%
"""

from __future__ import annotations

import warnings
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

__all__ = [
    "MODEL_ORDERS",
    "DEFAULT_ORDER",
    "generate_forecast",
    "evaluate_forecast",
    "forecast_index",
]

#: Versi model TIDAK didefinisikan di sini — lihat ``app/version.py`` yang
#: membaca ``model/VERSION``. Engine tidak boleh mendefinisikan versi agar
#: tidak ada dua sumber kebenaran.

#: Zona waktu tunggal untuk SELURUH sistem. Semua timestamp keluar memakai
#: offset eksplisit zona ini. Data masuk boleh berzona lain (SCADA UTC,
#: inverter WIB, dsb) — Dikonversi, bukan ditolak. Timestamp TANPA zona
#: selalu ditolak. Nilai ini wajib sama dengan ``preprocessing.DEFAULT_TIMEZONE``
#: (dijamin oleh test) dan dengan kontrak API §1.
DEFAULT_TIMEZONE = "Asia/Makassar"

#: Urutan ARIMA per zona waktu, dikunci dari notebook harian.
MODEL_ORDERS: dict[str, tuple[int, int, int]] = {
    "WIB": (3, 0, 0),
    "WITA": (1, 0, 2),
    "WIT": (2, 0, 1),
}

#: Kupang berada di WITA (UTC+8) — urutan default layanan.
DEFAULT_ORDER: tuple[int, int, int] = MODEL_ORDERS["WITA"]

MIN_HISTORY = 30
MAX_HORIZON = 30
#: Dinonaktifkan: tahap "pindah modul tanpa ubah logika" mewajibkan engine
#: menyetarakan output dengan skrip asli. Pembulatan 3 desimal jadi tugas
#: lapisan API (app/schemas.py, kontrak §3.1).
DECIMALS = None


def _as_frame(data: pd.DataFrame, name: str) -> pd.DataFrame:
    """Validasi struktur frame masukan tanpa menyentuh sumber data."""
    if not isinstance(data, pd.DataFrame):
        raise TypeError(f"{name} harus berupa pandas.DataFrame, bukan {type(data).__name__}")
    if data.empty:
        raise ValueError(f"{name} kosong")
    if not isinstance(data.index, pd.DatetimeIndex):
        raise TypeError(f"index {name} harus DatetimeIndex")
    return data


def _require_tz(data: pd.DataFrame, name: str) -> pd.DatetimeIndex:
    """Wajibkan timestamp ber-zona, lalu samakan ke ``DEFAULT_TIMEZONE``.

    Aturan zona tunggal seluruh sistem (kontrak API §1):

    - Timestamp **tanpa** zona → ditolak. Zona tidak pernah ditebak dari
      penampilan string, karena menebak salah membuat semua jam meleset.
    - Timestamp dengan zona → **dikonversi** ke ``DEFAULT_TIMEZONE``.
      Zona masukan boleh apa saja: SCADA UTC, inverter WIB, cuaca WIT.
      Mengonversi bukan menolak — sumber data berbeda zona adalah hal biasa.
    - Timestamp keluar selalu memakai offset eksplisit zona ini.

    Konversi ini yang mencegah bug integrasi paling umum: history UTC
    dipadukan dengan exog WIB tanpa dikonversi, sehingga kedua deret
    tampak "sudah ber-zona" tetapi selisih 8 jam.
    """
    if data.index.tz is None:
        raise ValueError(
            f"index {name} tanpa zona waktu harus ditolak. Berikan offset "
            "eksplisit (cth: +08:00). Zona tidak boleh ditebak. "
            "Timestamp naive membuat hasil forecast tidak dapat dibandingkan "
            "antar panggilan."
        )
    if str(data.index.tz) != DEFAULT_TIMEZONE:
        converted = data.copy()
        converted.index = data.index.tz_convert(DEFAULT_TIMEZONE)
        return converted.index
    return data.index


def _to_system_tz(data: pd.DataFrame, name: str) -> pd.DataFrame:
    """Pilih frame: tolak yang naive, konversi yang berzona berbeda."""
    _require_tz(data, name)  # menaikkan ValueError bila naive
    if str(data.index.tz) == DEFAULT_TIMEZONE:
        return data
    out = data.copy()
    out.index = data.index.tz_convert(DEFAULT_TIMEZONE)
    return out


def _prepare_target(historical_data: pd.DataFrame, target: str) -> pd.Series:
    if target not in historical_data.columns:
        raise KeyError(
            f"kolom target '{target}' tidak ada; tersedia: {sorted(historical_data.columns)}"
        )

    series = historical_data[target].astype("float64")

    if series.isna().any():
        raise ValueError(f"kolom target '{target}' mengandung NaN")
    if not np.isfinite(series.to_numpy()).all():
        raise ValueError(f"kolom target '{target}' mengandung nilai tak hingga")
    if (series < 0).any():
        raise ValueError(f"kolom target '{target}' mengandung nilai negatif")

    series = series.sort_index()
    if series.index.has_duplicates:
        dupes = series.index[series.index.duplicated()].unique()
        raise ValueError(f"index {target} duplikat: {list(dupes[:5])}")

    if len(series) < MIN_HISTORY:
        raise ValueError(
            f"history terlalu pendek: {len(series)} baris, minimal {MIN_HISTORY}"
        )

    return series


def _step_size(index: pd.DatetimeIndex) -> pd.Timedelta:
    """Interval langkah dari data, deterministik — bukan asumsi harian."""
    steps = index.to_series().diff().dropna()
    if steps.empty:
        raise ValueError("tidak bisa menentukan interval waktu dari index (butuh >= 2 baris)")
    if not (steps == steps.iloc[0]).all():
        gap = steps[steps != steps.iloc[0]]
        raise ValueError(
            "interval waktu tidak seragam; terdapat gap pada "
            f"{list(gap.index[:5])}. Resample atau rapikan history sebelum "
            "memanggil generate_forecast."
        )
    return pd.Timedelta(steps.iloc[0])


def forecast_index(last_time: pd.Timestamp, steps: int, step: pd.Timedelta) -> pd.DatetimeIndex:
    """Susun timestamp forecast tanpa mengandung nilai acak."""
    if steps < 1:
        raise ValueError(f"horizon harus >= 1, diterima {steps}")
    return pd.DatetimeIndex([last_time + step * (i + 1) for i in range(steps)], tz=last_time.tz)


def _split_exog(
    weather_data: pd.DataFrame | None,
    exog_cols: Sequence[str],
    history_index: pd.DatetimeIndex,
    future_index: pd.DatetimeIndex,
    target_name: str,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Sediakan exog historis + masa depan, atau keduanya ``None``.

    Bila ``weather_data`` diberikan, ia WAJIB mencakup seluruh jendela
    forecast. Exog parsial akan membuat model berbeda antar panggilan,
    sehingga hasil tidak lagi dapat direproduksi.
    """
    if weather_data is None or not exog_cols:
        return None, None

    weather = _as_frame(weather_data, "weather_data")
    weather = _to_system_tz(weather, "weather_data")
    weather = weather.sort_index()

    missing = [c for c in exog_cols if c not in weather.columns]
    if missing:
        raise KeyError(f"weather_data tidak punya kolom {missing}; tersedia: {sorted(weather.columns)}")

    # Target tidak boleh ikut jadi exog — itu kebocoran data (data leakage).
    overlap = [c for c in exog_cols if c == target_name]
    if overlap:
        raise ValueError(f"kolom target '{target_name}' dilarang ada di exog_cols")

    future_covered = weather.index.union(future_index).size == weather.index.size
    if not future_covered:
        raise ValueError(
            "weather_data tidak mencakup seluruh jendela forecast. "
            f"Butuh data sampai {future_index[-1]}, tersedia sampai {weather.index.max()}."
        )

    exog_hist = weather.reindex(history_index)[list(exog_cols)].astype("float64")
    exog_future = weather.reindex(future_index)[list(exog_cols)].astype("float64")

    if exog_hist.isna().any().any():
        gap = exog_hist.index[exog_hist.isna().any(axis=1)]
        raise ValueError(f"weather_data tidak selaras dengan history pada {list(gap[:5])}")
    if exog_future.isna().any().any():
        gap = exog_future.index[exog_future.isna().any(axis=1)]
        raise ValueError(f"weather_data tidak mengisi forecast window pada {list(gap[:5])}")

    return exog_hist.reset_index(drop=True), exog_future.reset_index(drop=True)


def generate_forecast(
    historical_data: pd.DataFrame,
    weather_data: pd.DataFrame | None = None,
    horizon: int = 24,
    *,
    target: str = "irradiance",
    order: tuple[int, int, int] = DEFAULT_ORDER,
    exog_cols: Iterable[str] = (),
    model_version: str | None = None,
) -> pd.DataFrame:
    """Jalankan forecast dan kembalikan DataFrame murni tanpa efek samping.

    Parameters
    ----------
    historical_data
        Frame ber-index ``DatetimeIndex`` tz-aware. Wajib memuat kolom
        ``target``. Diurutkan, divalidasi, dan tidak pernah diubah.
    weather_data
        Opsional. Bila diberikan, wajib mencakup jendela forecast penuh dan
        memuat semua ``exog_cols`` — dipakai sebagai regresor eksternal.
        Bila ``None``, model berjalan sebagai ARIMA murni.
    horizon
        Jumlah step ke depan, 1..30 untuk resolusi harian (kontrak v1).
    target
        Nama kolom yang diprediksi.
    order
        Urutan ARIMA ``(p,d,q)``. Default urutan WITA yang tervalidasi.
    exog_cols
        Kolom cuaca yang dipakai sebagai exog. Kosong = tanpa exog.
    model_version
        Label versi yang ditempelkan ke setiap baris output.

    Returns
    -------
    DataFrame dengan kolom ``forecast_time``, ``forecast_value``,
    ``model_version``. Satuan ``forecast_value`` mengikuti ``target``
    (kWh/hari bila target adalah energi harian) — lapisan API yang
    memetakannya ke nama field kontrak.

    Raises
    ------
    TypeError, ValueError, KeyError
        Bila struktur atau rentang input melanggar kontrak.
    """
    if not isinstance(horizon, (int, np.integer)) or isinstance(horizon, bool):
        raise TypeError(f"horizon harus int, bukan {type(horizon).__name__}")
    if horizon < 1 or horizon > MAX_HORIZON:
        raise ValueError(f"horizon harus 1..{MAX_HORIZON}, diterima {horizon}")

    history = _as_frame(historical_data, "historical_data")
    history = _to_system_tz(history, "historical_data")
    series = _prepare_target(history, target)

    # Versi diresolusi di sini (bukan sebagai default parameter) agar selalu
    # membaca ``model/VERSION`` yang terbaru, walau import sudah lama.
    if model_version is None:
        from app.version import model_version as _current_version

        model_version = _current_version()

    step = _step_size(series.index)
    future_index = forecast_index(series.index[-1], int(horizon), step)

    exog_hist, exog_future = _split_exog(
        weather_data, tuple(exog_cols), series.index, future_index, target
    )

    # ARIMA dari statsmodels deterministik: optimizer MLE memakai start params
    # heuristik tetap, tanpa seed acak. try/except menutup peringatan numerik
    # tanpa mencetak apa pun ke stdout.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            fitted = ARIMA(
                series.to_numpy(),
                order=tuple(order),
                exog=exog_hist,
            ).fit()
            values = np.asarray(fitted.forecast(int(horizon), exog=exog_future))
        except Exception as exc:  # gagal konvergen / orde tak valid
            raise RuntimeError(f"model ARIMA{tuple(order)} gagal dihitung: {exc}") from exc

    return pd.DataFrame(
        {
            "forecast_time": future_index,
            "forecast_value": values.astype("float64"),
            "model_version": str(model_version),
        }
    )


def evaluate_forecast(actual: Sequence[float], predicted: Sequence[float]) -> dict[str, float]:
    """Hitung MAE/RMSE/MAPE tanpa efek samping.

    Tanpa pembulatan agar metrik dapat dibandingkan eksak antar run.
    """
    y_true = np.asarray(actual, dtype="float64")
    y_pred = np.asarray(predicted, dtype="float64")

    if y_true.shape != y_pred.shape:
        raise ValueError(f"bentuk tidak sama: {y_true.shape} vs {y_pred.shape}")
    if y_true.size == 0:
        raise ValueError("data evaluasi kosong")

    residual = y_true - y_pred
    nonzero = y_true != 0

    mape = float(np.mean(np.abs(residual[nonzero] / y_true[nonzero])) * 100) if nonzero.any() else float("nan")

    return {
        "mae": float(np.mean(np.abs(residual))),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mape": mape,
        "n": int(y_true.size),
    }
