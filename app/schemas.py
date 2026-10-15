"""Kontrak Pydantic API v1.

Sumber kebenaran semantik: ``../KONTRAK_API_v1.md``. Field di sini harus
berubah bersama dokumen kontrak; jangan menambah alias field tersembunyi.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SITE_TZ = ZoneInfo("Asia/Makassar")

#: Semver dua digit (v1.0) ATAU tiga digit (v1.0.0) — keduanya sah menurut
#: semver. Pola ini harus selaras dengan ``app/version.SEMVER_PATTERN``;
#: difungsi test_version.py memeriksa kesetaraannya.
SEMVER = r"^v\d+\.\d+(\.\d+)?$"


def site_now() -> datetime:
    """Waktu server sekarang, dalam zona site — dipakai ``generated_at``."""
    return datetime.now(tz=SITE_TZ)


class StrictSchema(BaseModel):
    # protected_namespaces dikosongkan: kontrak memakai field `model_version`
    # dan `model_loaded` (§3.1, §5.6). Tanpa ini Pydantic memunculkan warning
    # setiap model didefinisikan — memenuhi log container tanpa memberi nilai.
    model_config = ConfigDict(
        extra="forbid", frozen=True, protected_namespaces=()
    )


def _must_be_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset")
    return value


def _is_site_time(value: datetime, field_name: str) -> datetime:
    value = _must_be_aware(value, field_name)
    return value.astimezone(SITE_TZ)


class HistoryPoint(StrictSchema):
    timestamp: datetime
    #: strict: angka harus angka asli. Tanpa ini, Pydantic mengonversi
    #: "180.0" → 180.0 dan request yang salah tipe lolos tanpa error —
    #: client tidak pernah tahu ia salah (kontrak §3: salah tipe = ditolak).
    value: float = Field(ge=0, strict=True)

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            # RFC 3339: 'Z' or explicit +/-hh:mm; reject naive ISO strings.
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-")):
                raise ValueError("timestamp must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "timestamp")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _is_site_time(value, "timestamp")

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("value must be finite")
        return value


class ExogPoint(StrictSchema):
    timestamp: datetime
    values: dict[str, float]

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-")):
                raise ValueError("timestamp must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "timestamp")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _is_site_time(value, "timestamp")

    @field_validator("values")
    @classmethod
    def finite_values(cls, values: dict[str, float]) -> dict[str, float]:
        if not values:
            raise ValueError("exog values must not be empty")
        if any(not isfinite(v) for v in values.values()):
            raise ValueError("exog values must be finite")
        return values


class ExogInput(StrictSchema):
    columns: list[str] = Field(min_length=1)
    points: list[ExogPoint] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_columns(self) -> "ExogInput":
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("exog columns must be unique")
        for point in self.points:
            missing = set(self.columns) - point.values.keys()
            if missing:
                raise ValueError(f"exog point missing columns: {sorted(missing)}")
        times = [p.timestamp.astimezone(timezone.utc) for p in self.points]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("exog timestamps must be unique and ascending")
        return self


class ForecastRequest(StrictSchema):
    site_id: str = Field(pattern=r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$")
    target: Literal["irradiance"] = "irradiance"
    resolution: Literal["daily"] = "daily"
    #: strict: "7" (string) dan 7.5 (float) harus DITOLAK, bukan diam-diam
    #: dinomalkan. Regresi teruji: Pydantic non-strict menerima keduanya dan
    #: merespons 200 seolah request valid.
    horizon: int = Field(ge=1, le=30, strict=True)
    history: list[HistoryPoint] = Field(min_length=30)
    exog: ExogInput | None = None

    @model_validator(mode="after")
    def validate_history(self) -> "ForecastRequest":
        times = [p.timestamp.astimezone(timezone.utc) for p in self.history]
        if times != sorted(times):
            raise ValueError("history timestamps must be ascending")
        if len(times) != len(set(times)):
            raise ValueError("history timestamps must be unique")
        if len(times) > 1:
            diffs = [b - a for a, b in zip(times, times[1:])]
            if len(set(diffs)) != 1:
                raise ValueError("history timestamps must have regular intervals")
        return self


class ForecastPoint(StrictSchema):
    forecast_time: datetime
    forecast_value: float = Field(ge=0)
    model_version: str = Field(pattern=SEMVER)

    @field_validator("forecast_time", mode="before")
    @classmethod
    def timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-")):
                raise ValueError("forecast_time must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "forecast_time")
        return value

    @field_validator("forecast_time")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _is_site_time(value, "forecast_time")

    @field_validator("forecast_value")
    @classmethod
    def finite_forecast(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("forecast_value must be finite")
        return round(value, 3)


class ForecastResponse(StrictSchema):
    site_id: str
    target: Literal["irradiance"]
    resolution: Literal["daily"]
    unit: Literal["W/m2"]
    timezone: Literal["Asia/Makassar"] = "Asia/Makassar"
    ts: datetime
    generated_at: datetime
    horizon: int = Field(ge=1, le=30)
    model_version: str = Field(pattern=SEMVER)
    order: str
    forecast: list[ForecastPoint]
    forecast_id: str | None = None

    @field_validator("ts", "generated_at", mode="before")
    @classmethod
    def timestamps_have_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-")):
                raise ValueError("timestamp must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "timestamp")
        return value

    @field_validator("ts", "generated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _is_site_time(value, "timestamp")

    @model_validator(mode="after")
    def validate_forecast(self) -> "ForecastResponse":
        if len(self.forecast) != self.horizon:
            raise ValueError("len(forecast) must equal horizon")
        times = [p.forecast_time.astimezone(timezone.utc) for p in self.forecast]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("forecast timestamps must be unique and ascending")
        return self


class ForecastSummary(StrictSchema):
    forecast_id: str
    site_id: str
    target: str
    resolution: str
    unit: str
    ts: datetime
    generated_at: datetime
    horizon: int
    model_version: str
    forecast: list[ForecastPoint] | None = None


class PageInfo(StrictSchema):
    limit: int
    next_cursor: str | None = None
    has_more: bool


class ForecastHistoryResponse(StrictSchema):
    items: list[ForecastSummary]
    page: PageInfo


class ModelInfoResponse(StrictSchema):
    model_version: str
    target: str
    resolution: str
    #: Konteks satuan (kontrak §3.1): konsumen tahu satuan tanpa menebak
    #: dari target. Menambah field = non-breaking (§9.2).
    unit: str = ""
    algorithm: str
    order: list[int]
    timezone: str
    history_min_rows: int
    horizon_max: int
    #: Jumlah baris data yang benar-benar dipakai melatih artefak saat ini.
    #: Sumber: model/artifacts.json → training_rows (lihat app/artifacts.py).
    #: Konteks telusur: konsumen bisa menilai apakah angka MAE/MAPE berasal
    #: dari dataset yang cukup besar, tanpa membuka file.
    training_rows: int = Field(ge=0)
    trained_at: datetime | None = None
    training_range: dict[str, datetime] | None = None
    features: list[str] = Field(default_factory=list)
    exog_supported: bool
    deterministic: bool
    evaluation_status: Literal["GO", "NO_GO", "UNKNOWN"]
    engine: str
    preprocessing: str


class HorizonMetrics(StrictSchema):
    horizon: int = Field(ge=1, le=30)
    mae: float = Field(ge=0)
    rmse: float = Field(ge=0)
    mape: float = Field(ge=0)
    skill: float


class ThresholdsResponse(StrictSchema):
    max_mae: float
    max_mape: float
    max_mape_long: float
    min_skill: float
    primary_horizon: int


class PerformanceResponse(StrictSchema):
    site_id: str
    target: str
    resolution: str
    unit: str
    model_version: str
    #: Waktu backtest DIBUAT, bukan waktu request. Sumber: laporan
    #: ``generated_at`` (scripts/run_evaluation.py). Bila laporan tak ada,
    #: nilainya `null` — lebih jujur daripada `site_now()` yang akan membuat
    #: "run terakhir" tampak selalu baru setiap halaman dibuka.
    evaluated_at: datetime | None = None
    #: Jumlah baris dataset yang dipakai backtest (n_rows di laporan).
    #: Berbeda dari n_samples (jumlah origin forecast yang dinilai).
    data_rows: int = Field(ge=0, default=0)
    evaluation_range: dict[str, datetime] | None = None
    n_samples: int = Field(ge=0)
    thresholds: ThresholdsResponse
    status: Literal["GO", "NO_GO", "UNKNOWN"]
    failures: list[str] = Field(default_factory=list)
    horizons: list[HorizonMetrics] = Field(default_factory=list)


class HealthResponse(StrictSchema):
    status: Literal["ok", "degraded", "unavailable"]
    service_version: str
    timestamp: datetime
    uptime_seconds: int = Field(ge=0)
    model_loaded: bool

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-")):
                raise ValueError("timestamp must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "timestamp")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _is_site_time(value, "timestamp")


class ErrorDetail(StrictSchema):
    field: str
    issue: str
    value: str | int | float | bool | None = None


class ErrorPayload(StrictSchema):
    code: str
    message: str
    details: list[ErrorDetail] | None = None
    request_id: str


class ErrorEnvelope(StrictSchema):
    error: ErrorPayload


#: CATATAN: model query Pydantic untuk latest/history DIHAPUS.
#: Route memakai ``Query(alias=...)`` eksplisit di signature (pola
#: ``get_actual``). Model ``Depends()`` dengan alias Pydantic tidak bekerja:
#: FastAPI mencocokkan nama FIELD (``from_``), bukan alias (``from``) —
#: ``?from=...`` diam-diam diabaikan. Lihat regresi di
#: ``tests/test_contract_fields.py::test_history_from_to_filters_apply``.
