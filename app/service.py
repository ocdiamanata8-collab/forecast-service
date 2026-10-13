"""Layanan forecast — logika bisnis di luar lapisan HTTP.

Kenapa modul ini ada (dan kenapa endpoint tidak boleh memilikinya):

    Fase 4: scheduler rolling forecast
        │  ── memanggil service.run_forecast(...)  ← tanpa HTTP, tanpa FastAPI
        │
    Endpoint POST /api/v1/forecast
        └── memanggil fungsi YANG SAMA

Bila logika di bawah ini tinggal di ``app/api/forecast.py``, scheduler harus
membentuk ``ForecastRequest`` Pydantic dan memanggil helper HTTP hanya untuk
menjalankan model — logika terkunci di web layer dan tidak terpakai ulang.

Modul ini TIDAK mengimpor ``fastapi`` maupun ``pydantic``. Input berupa
tipe data polos (tuple, datetime, dict), output berupa dataclass. Jadi bisa
dipanggil dari script, job, maupun endpoint.

Aturan kontrak yang dijalankan DI SINI (bukan di endpoint):

- Target dicek terhadap ``model/artifacts.json``; urutan exog mengikuti
  urutan training, bukan urutan permintaan.
- Scaling request disamakan dengan scaling training.
- ``prepare_dataset`` (resample → interpolasi → validasi) sebelum engine.
- Klip ``forecast_value >= 0`` (C-05) + pembulatan 3 desimal (§3.1).
- Pemetaan ``target`` → ``unit`` dan label ``order``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from app import engine, preprocessing as pp
from app.artifacts import ArtifactError, apply_scaling, load_artifacts, reorder_exog_frame
from app.preprocessing import DEFAULT_TIMEZONE
from app.version import model_version as current_model_version

__all__ = [
    "ForecastError",
    "ExogData",
    "ForecastPointData",
    "ForecastOutcome",
    "run_forecast",
    "UNIT_BY_TARGET",
]

#: Satuan resmi per target (kontrak §4). Menambah baris baru = non-breaking;
#: mengubah nilai baris yang ada = breaking (kontrak §9.1).
UNIT_BY_TARGET: dict[str, str] = {"irradiance": "W/m2"}

SITE_TZ = ZoneInfo(DEFAULT_TIMEZONE)


class ForecastError(Exception):
    """Galat domain — kode error kontrak, tanpa dependensi HTTP.

    ``app/api/forecast.py`` memetakan ini ke ``FastApiError`` (envelope §3.3);
    scheduler memakai ``exc.code`` apa adanya.
    """

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ExogData:
    """Data cuaca/exog: kolom + baris (timestamp, nilai per kolom)."""

    columns: tuple[str, ...]
    rows: tuple[tuple[datetime, Mapping[str, float]], ...]


@dataclass(frozen=True)
class ForecastPointData:
    forecast_time: datetime
    forecast_value: float
    model_version: str


@dataclass(frozen=True)
class ForecastOutcome:
    """Hasil layanan — cukup untuk scheduler menyimpan, atau API membungkus."""

    site_id: str
    target: str
    resolution: str
    unit: str
    timezone: str
    ts: datetime
    generated_at: datetime
    horizon: int
    model_version: str
    order: str
    points: tuple[ForecastPointData, ...]


def _site_now() -> datetime:
    return datetime.now(tz=SITE_TZ)


def run_forecast(
    *,
    site_id: str,
    history: Sequence[tuple[datetime, float]],
    horizon: int,
    target: str = "irradiance",
    resolution: str = "daily",
    exog: ExogData | None = None,
    generated_at: datetime | None = None,
) -> ForecastOutcome:
    """Jalankan forecast dari data polos → :class:`ForecastOutcome`.

    Parameters
    ----------
    site_id
        Identitas site (mis. ``PLTS_KUPANG``).
    history
        Urutan ``(timestamp tz-aware, value)`` — wajib naik, langkah teratur.
    horizon
        Jumlah step ke depan (1..30 untuk harian).
    target, resolution
        Kontrak §4 / §3.2.
    exog
        Opsional. Kolomnya **tidak harus** dalam urutan training — modul ini
        menyusunnya ulang mengikuti ``artifacts.exog_columns``.
    generated_at
        Waktu pembuatan; ``None`` = waktu server. Scheduler bisa menyuntikkan
        agar hasil tersimpan membawa waktu yang benar.

    Raises
    ------
    ForecastError
        Galat domain dengan ``code`` kontrak (422/500 sesuai ``status``).
    """
    arts = load_artifacts()
    if target != arts.target:
        raise ForecastError(
            "E_TARGET_UNAVAILABLE",
            f"target '{target}' tidak sesuai artefak model ('{arts.target}')",
        )
    if resolution != arts.resolution:
        raise ForecastError(
            "E_VALIDATION_FAILED",
            f"resolution '{resolution}' tidak didukung artefak ('{arts.resolution}')",
        )

    if not history:
        raise ForecastError("E_HISTORY_TOO_SHORT", "history kosong")

    # Cek tz SEBELUM konstruksi index: pd.DatetimeIndex(..., tz=...) akan
    # MENGLOKALISASI timestamp naive (menebak zona), bukan menolaknya — dan
    # §1.5 melarang menebak. Tanpa cek ini, scheduler yang memanggil service
    # langsung dapat perilaku berbeda dari endpoint.
    for ts, _ in history:
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ForecastError(
                "E_TZ_REQUIRED",
                "timestamp history tanpa offset eksplisit — zona tidak boleh ditebak",
            )

    idx = pd.DatetimeIndex([ts for ts, _ in history]).tz_convert(DEFAULT_TIMEZONE)
    hist = pd.DataFrame({target: [v for _, v in history]}, index=idx)
    # Skala request harus sama dengan skala training (identitas bila tanpa normalisasi).
    hist = apply_scaling(hist, arts)

    weather: pd.DataFrame | None = None
    if exog is not None:
        for ts, _ in exog.rows:
            if ts.tzinfo is None or ts.utcoffset() is None:
                raise ForecastError(
                    "E_TZ_REQUIRED",
                    "timestamp exog tanpa offset eksplisit — zona tidak boleh ditebak",
                )
        ex_idx = pd.DatetimeIndex([ts for ts, _ in exog.rows]).tz_convert(
            DEFAULT_TIMEZONE
        )
        values_by_col = {
            col: [values.get(col) for _, values in exog.rows] for col in exog.columns
        }
        if any(v is None for col_vals in values_by_col.values() for v in col_vals):
            raise ForecastError(
                "E_EXOG_MISSING_COLUMN",
                "ada baris exog yang tidak memuat seluruh kolom yang disebut",
            )
        try:
            # Frame dibentuk ulang mengikuti urutan training — bukan urutan
            # permintaan. Inilah kunci anti "hasil API aneh".
            weather = reorder_exog_frame(
                list(exog.columns), {k: list(v) for k, v in values_by_col.items()}, arts
            )
        except ArtifactError as exc:
            message = str(exc)
            code = (
                "E_EXOG_MISSING_COLUMN" if "tidak lengkap" in message
                else "E_VALIDATION_FAILED"
            )
            raise ForecastError(code, message) from exc
        weather.index = ex_idx
        weather = apply_scaling(weather, arts)

    try:
        prepared = pp.prepare_dataset(
            hist, target=target, freq="1D", timezone=DEFAULT_TIMEZONE
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ForecastError("E_VALIDATION_FAILED", str(exc)) from exc

    exog_cols = arts.exog_columns if exog is not None else ()
    try:
        fc = engine.generate_forecast(
            prepared.frame,
            weather_data=weather,
            horizon=horizon,
            target=target,
            exog_cols=exog_cols,
        )
    except ValueError as exc:
        raise ForecastError("E_VALIDATION_FAILED", str(exc)) from exc
    except RuntimeError as exc:
        raise ForecastError("E_MODEL_FAILED", str(exc), status=500) from exc

    if target not in UNIT_BY_TARGET:
        raise ForecastError(
            "E_TARGET_UNAVAILABLE",
            f"target '{target}' tidak punya satuan terdaftar (kontrak §4)",
        )

    points: list[ForecastPointData] = []
    for row in fc.itertuples():
        # Aturan kontrak C-05 (>= 0) & pembulatan 3 desimal (§3.1) dijalankan
        # DI SINI supaya scheduler dan API menghasilkan angka yang sama.
        value = float(row.forecast_value)
        if not math.isfinite(value):
            value = 0.0
        points.append(
            ForecastPointData(
                forecast_time=row.forecast_time,
                forecast_value=round(max(value, 0.0), 3),
                model_version=str(row.model_version),
            )
        )

    return ForecastOutcome(
        site_id=site_id,
        target=target,
        resolution=resolution,
        unit=UNIT_BY_TARGET[target],
        timezone=DEFAULT_TIMEZONE,
        ts=history[-1][0],
        generated_at=generated_at or _site_now(),
        horizon=int(horizon),
        model_version=current_model_version(),
        order=f"ARIMA{engine.DEFAULT_ORDER}",
        points=tuple(points),
    )
