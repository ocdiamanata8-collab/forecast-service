"""Endpoint forecast: POST on-demand, GET latest, GET history (kontrak §5.1–5.3).

Endpoint TIDAK menyimpan logika bisnis. Seluruh urutan
(validasi artefak → bangun frame → reorder exog → scaling → prepare →
engine → klip/pembulatan → satuan) ada di :mod:`app.service` dan dapat
dipanggil scheduler Fase 4 tanpa HTTP. Tugas file ini hanya:

    1. memvalidasi payload (Pydantic),
    2. memanggil service,
    3. membungkus hasil dalam response schema,
    4. memetakan galat domain ``ForecastError`` ke envelope §3.3.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, Depends, Query, Request

from app import actuals as actual_data
from app import engine, storage
from app import service
from app.actual_schemas import ActualPoint, ActualResponse
from app.actuals import ActualsUnavailable
from app.schemas import (
    ForecastHistoryQuery,
    ForecastHistoryResponse,
    ForecastPoint,
    ForecastRequest,
    ForecastResponse,
    ForecastSummary,
    PageInfo,
    site_now,
)
from app.security import (
    FastApiError,
    assert_known_site,
    require_api_key,
    require_model_ready,
)

router = APIRouter(prefix="/api/v1", tags=["forecast"])


@router.get("/forecast/actual", response_model=ActualResponse)
def get_actual(
    request: Request,
    site_id: str = Query(pattern=r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    _: str = Depends(require_api_key),
) -> ActualResponse:
    """§5.7 — data irradiance aktual per titik untuk engineer/monitoring.

    Sumber aktual dibaca server-side dari ``data/actuals.csv``; browser
    TIDAK mengakses CSV proyek atau filesystem. Endpoint ini tidak butuh
    model loaded — data historis tetap terbaca saat model sedang reload.

    Filter ``from``/``to`` harus tz-aware; tanpa filter default = 60 hari
    terakhir menurut kalender site. Rentang tanpa observasi → 200 + ``points:[]``
    (rentang valid yang kosong bukan resource yang tak ada).
    """
    assert_known_site(request, site_id)

    now = site_now()
    start = from_ or (to - pd.Timedelta(days=60) if to else now - pd.Timedelta(days=60))
    end = to or now
    # FastAPI/Pydantic bisa menerima datetime-naive dari query string;
    # kontrak §1.5 melarang menebak zona, jadi cek eksplisit di boundary.
    for name, value in (("from", start), ("to", end)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise FastApiError(
                "E_TZ_REQUIRED",
                f"{name} must include an explicit UTC offset; timezone must not be guessed",
                status=422,
                details=[{"field": name, "issue": "timezone_offset_required", "value": value.isoformat()}],
            )
    if end.astimezone(timezone.utc) < start.astimezone(timezone.utc):
        raise FastApiError(
            "E_TIME_RANGE_INVALID",
            "to must be greater than or equal to from",
            status=422,
            details=[{"field": "to", "issue": "before_from", "value": end.isoformat()}],
        )

    try:
        frame = actual_data.load_actual_series(site_id=site_id, start=start, end=end)
    except ActualsUnavailable as exc:
        raise FastApiError("E_SERVICE_UNAVAILABLE", str(exc), status=503) from exc
    except ValueError as exc:
        raise FastApiError("E_TIME_RANGE_INVALID", str(exc), status=422) from exc

    points = [
        ActualPoint(timestamp=ts.to_pydatetime(), actual_value=float(row.actual_irradiance))
        for ts, row in zip(frame.index, frame.itertuples())
    ]
    return ActualResponse(
        site_id=site_id,
        from_=start,
        to=end,
        source=actual_data.ACTUALS_SOURCE,
        points=points,
        n=len(points),
    )


def _outcome_to_response(
    outcome: service.ForecastOutcome, *, forecast_id: str | None = None
) -> ForecastResponse:
    """Bungkus hasil service dalam schema response kontrak §3.1/§3.2."""
    return ForecastResponse(
        site_id=outcome.site_id,
        target=outcome.target,  # type: ignore[arg-type]
        resolution=outcome.resolution,  # type: ignore[arg-type]
        unit=outcome.unit,  # type: ignore[arg-type]
        timezone=outcome.timezone,  # type: ignore[arg-type]
        ts=outcome.ts,
        generated_at=outcome.generated_at,
        horizon=outcome.horizon,
        model_version=outcome.model_version,
        order=outcome.order,
        forecast=[
            ForecastPoint(
                forecast_time=p.forecast_time,
                forecast_value=p.forecast_value,
                model_version=p.model_version,
            )
            for p in outcome.points
        ],
        forecast_id=forecast_id,
    )


def _request_to_service(req: ForecastRequest) -> dict[str, object]:
    """Peta payload Pydantic → argumen polos ``service.run_forecast``."""
    exog: service.ExogData | None = None
    if req.exog is not None:
        exog = service.ExogData(
            columns=tuple(req.exog.columns),
            rows=tuple(
                (p.timestamp, dict(p.values)) for p in req.exog.points
            ),
        )
    return {
        "site_id": req.site_id,
        "history": tuple((p.timestamp, p.value) for p in req.history),
        "horizon": req.horizon,
        "target": req.target,
        "resolution": req.resolution,
        "exog": exog,
    }


def run_forecast(req: ForecastRequest) -> ForecastResponse:
    """Adapter HTTP↔service. Dipakai endpoint dan test; logika di ``app.service``."""
    try:
        outcome = service.run_forecast(**_request_to_service(req))
    except service.ForecastError as exc:
        raise FastApiError(exc.code, exc.message, status=exc.status) from exc
    return _outcome_to_response(outcome)


@router.post("/forecast", response_model=ForecastResponse, response_model_exclude_none=True)
def post_forecast(
    request: Request,
    payload: ForecastRequest,
    idempotency_key: str | None = None,
    _: str = Depends(require_api_key),
    __: None = Depends(require_model_ready),
) -> ForecastResponse:
    """§5.1 — forecast on-demand; deterministik, tidak menyimpan."""
    # site_id dari body divalidasi sama seperti query: site yang tidak
    # terdaftar adalah salah konfigurasi, jauh lebih baik gagal di sini
    # daripada menghasilkan forecast yang tidak pernah bisa dicocokkan
    # dengan site manapun di platform monitoring.
    assert_known_site(request, payload.site_id)
    return run_forecast(payload)


@router.get("/forecast/latest", response_model=ForecastResponse)
def get_latest(
    request: Request,
    site_id: str = Query(pattern=r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$"),
    _: str = Depends(require_api_key),
) -> ForecastResponse:
    """§5.2 — forecast terakhir untuk monitoring."""
    assert_known_site(request, site_id)
    store: storage.ForecastStore = request.app.state.store
    stored = store.latest(site_id)
    if stored is None:
        # Site terdaftar tapi belum ada forecast: bukan kesalahan konfigurasi.
        raise FastApiError(
            "E_NO_FORECAST",
            f"site {site_id} is known but no forecast has been generated yet "
            "(scheduler may not have run)",
            status=404,
        )
    return _response_from_stored(stored)


@router.get(
    "/forecast/history",
    response_model=ForecastHistoryResponse,
    response_model_exclude_none=True,
)
def get_history(
    request: Request,
    q: ForecastHistoryQuery = Depends(),
    _: str = Depends(require_api_key),
) -> ForecastHistoryResponse:
    """§5.3 — riwayat tersimpan, cursor pagination."""
    assert_known_site(request, q.site_id)
    store: storage.ForecastStore = request.app.state.store
    try:
        items, next_cursor = store.history(
            site_id=q.site_id,
            start=q.from_,
            end=q.to,
            limit=q.limit,
            cursor=q.cursor,
        )
    except ValueError as exc:
        message = str(exc)
        if "filter" in message:
            raise FastApiError("E_CURSOR_FILTER_MISMATCH", message, status=422) from exc
        raise FastApiError("E_CURSOR_INVALID", message, status=400) from exc

    summaries = [
        ForecastSummary(
            forecast_id=i.forecast_id,
            site_id=i.site_id,
            target=i.target,
            resolution=i.resolution,
            unit=i.unit,
            ts=i.ts,
            generated_at=i.generated_at,
            horizon=i.horizon,
            model_version=i.model_version,
            forecast=list(i.forecast) if q.include_points else None,
        )
        for i in items
    ]
    return ForecastHistoryResponse(
        items=summaries,
        page=PageInfo(
            limit=q.limit,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        ),
    )


def _response_from_stored(item: storage.StoredForecast) -> ForecastResponse:
    return ForecastResponse(
        site_id=item.site_id,
        target=item.target,  # type: ignore[arg-type]
        resolution=item.resolution,  # type: ignore[arg-type]
        unit=item.unit,  # type: ignore[arg-type]
        ts=item.ts,
        generated_at=item.generated_at,
        horizon=item.horizon,
        model_version=item.model_version,
        order=f"ARIMA{engine.MODEL_ORDERS['WITA']}",
        forecast=list(item.forecast),
        forecast_id=item.forecast_id,
    )
