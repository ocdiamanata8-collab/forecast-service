"""Schema response untuk endpoint actual — selaras KONTRAK_API_v1.md §5.7."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas import SITE_TZ, StrictSchema, _must_be_aware


class ActualPoint(StrictSchema):
    """Satu pengamatan aktual (bukan forecast, bukan output model)."""

    timestamp: datetime
    actual_value: float = Field(ge=0, strict=True)

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-") ):
                raise ValueError("timestamp must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "timestamp")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(SITE_TZ)

    @field_validator("actual_value")
    @classmethod
    def finite_actual(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("actual_value must be finite")
        return round(value, 3)


class ActualResponse(StrictSchema):
    """Response deret aktual harian untuk site dan rentang tanggal."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    site_id: str
    target: Literal["irradiance"] = "irradiance"
    unit: Literal["W/m2"] = "W/m2"
    timezone: Literal["Asia/Makassar"] = "Asia/Makassar"
    from_: datetime = Field(alias="from")
    to: datetime
    source: str
    points: list[ActualPoint]
    n: int = Field(ge=0)

    @field_validator("from_", "to", mode="before")
    @classmethod
    def range_timestamp_has_offset(cls, value: object) -> object:
        if isinstance(value, str):
            if not (value.endswith("Z") or (len(value) >= 6 and value[-6] in "+-") ):
                raise ValueError("date filter must include an explicit UTC offset")
        elif isinstance(value, datetime):
            _must_be_aware(value, "date filter")
        return value

    @field_validator("from_", "to")
    @classmethod
    def normalize_range(cls, value: datetime) -> datetime:
        return value.astimezone(SITE_TZ)

    @model_validator(mode="after")
    def validate_points(self) -> "ActualResponse":
        if self.to < self.from_:
            raise ValueError("to must be greater than or equal to from")
        if self.n != len(self.points):
            raise ValueError("n must equal len(points)")
        times = [p.timestamp for p in self.points]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("actual timestamps must be unique and ascending")
        return self
