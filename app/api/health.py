"""Endpoint health (§5.6) — satu-satunya endpoint tanpa API key."""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Request

from app.schemas import HealthResponse, site_now
from app.version import FALLBACK_VERSION, model_version

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    """§5.6 — status layanan. Tanpa ``X-API-Key`` (kontrak §1.1)."""
    app = request.app
    started_at = getattr(app.state, "started_at", None)
    uptime = int(time.monotonic() - started_at) if started_at else 0
    model_loaded = bool(getattr(app.state, "model_loaded", False))
    version_ok = getattr(app.state, "version_ok", True)
    artifacts_ok = getattr(app.state, "artifacts_ok", True)

    if not (version_ok and artifacts_ok):
        status = "degraded"  # jalan, tapi artefak/versi tak terbaca (§5.6)
    elif not model_loaded:
        status = "unavailable"
    else:
        status = "ok"
    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service_version=FALLBACK_VERSION if not version_ok else model_version(),
        timestamp=site_now(),
        uptime_seconds=uptime,
        model_loaded=model_loaded,
    )
