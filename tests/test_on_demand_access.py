"""Pemisahan jalur on-demand vs operasional (POST §5.1 vs GET latest §5.2).

    pytest tests/test_on_demand_access.py -q

- Konsumen ``monitoring`` ditolak ``403 E_FORBIDDEN`` di POST /forecast.
- Konsumen yang sama tetap boleh GET /forecast/latest (baca penyimpanan).
- Kuota on-demand (bawaan 6/menit) terpisah dari §1.3 — mencegah polling engine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.schemas import ForecastPoint
from app.storage import items_from_response
from app.version import model_version

SITE = "PLTS_KUPANG"
MONITORING_KEY = "monitor-test-key-0123456789"


def history_payload(n: int = 60) -> list[dict[str, object]]:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    v = np.clip(
        180 + 60 * np.sin(2 * np.pi * idx.dayofyear.to_numpy(float) / 365.25),
        0,
        None,
    )
    return [{"timestamp": t.isoformat(), "value": float(x)} for t, x in zip(idx, v)]


def forecast_body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "horizon": 7,
        "history": history_payload(),
    }
    base.update(over)
    return base


def _register_monitoring_key() -> None:
    from app.security import get_registry

    get_registry().keys[MONITORING_KEY] = "monitoring"


def test_monitoring_blocked_on_post_forecast(client):
    _register_monitoring_key()
    r = client.post(
        "/api/v1/forecast",
        json=forecast_body(),
        headers={"X-API-Key": MONITORING_KEY},
    )
    assert r.status_code == 403, r.text[:300]
    err = r.json()["error"]
    assert err["code"] == "E_FORBIDDEN"
    assert "latest" in err["message"].lower()


def test_monitoring_can_poll_latest(client):
    _register_monitoring_key()
    base = pd.Timestamp("2026-10-01T06:00:00+08:00")
    pt = ForecastPoint(
        forecast_time=base + pd.Timedelta(days=1),
        forecast_value=212.5,
        model_version=model_version(),
    )
    client.app.state.store.save(
        items_from_response(
            forecast_id="00000000-0000-4000-8000-000000000002",
            site_id=SITE,
            target="irradiance",
            resolution="daily",
            unit="W/m2",
            ts=base,
            generated_at=base,
            model_version=model_version(),
            points=[pt],
        )
    )
    r = client.get(
        "/api/v1/forecast/latest",
        params={"site_id": SITE},
        headers={"X-API-Key": MONITORING_KEY},
    )
    assert r.status_code == 200, r.text[:300]
    assert r.json()["site_id"] == SITE


def test_on_demand_rate_limit_headers(client, auth_headers):
    last = None
    for _ in range(7):
        last = client.post(
            "/api/v1/forecast",
            json=forecast_body(history=[]),
            headers=auth_headers,
        )
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429, (
        f"on-demand limit tidak terpicu (status {last.status_code if last else '-'}): "
        f"{last.text[:200] if last else ''}"
    )
    assert last.json()["error"]["code"] == "E_RATE_LIMITED"
    assert last.headers.get("X-RateLimit-Limit") == "6"
    assert last.headers.get("X-RateLimit-Remaining") == "0"
    assert last.headers.get("Retry-After")
    assert "latest" in last.json()["error"]["message"].lower()


def test_legacy_key_still_allowed_post_forecast(client, auth_headers):
    """Key tanpa label monitoring (legacy dari conftest) tetap bisa uji POST."""
    r = client.post(
        "/api/v1/forecast",
        json=forecast_body(),
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text[:300]
    assert r.json()["site_id"] == SITE
