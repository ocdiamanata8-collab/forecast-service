"""Contract tests for GET /api/v1/forecast/actual (KONTRAK_API_v1.md §5.7)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO_KEY = "dev-key-change-me-0123456789"
HEADERS = {"X-API-Key": DEMO_KEY}
URL = "/api/v1/forecast/actual"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def known_week():
    return {
        "site_id": "PLTS_KUPANG",
        "from": "2024-03-01T00:00:00+08:00",
        "to": "2024-03-07T00:00:00+08:00",
    }


def test_actual_known_week_returns_seven_wita_points(client):
    r = client.get(URL, params=known_week(), headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["site_id"] == "PLTS_KUPANG"
    assert data["target"] == "irradiance"
    assert data["unit"] == "W/m2"
    assert data["timezone"] == "Asia/Makassar"
    assert data["source"] == "data/actuals.csv"  # no filesystem path leaks
    assert data["n"] == len(data["points"]) == 7
    assert data["from"].endswith("+08:00")
    assert data["to"].endswith("+08:00")
    assert data["points"][0]["timestamp"] == "2024-03-01T00:00:00+08:00"
    assert data["points"][0]["actual_value"] == pytest.approx(199.226, abs=0.001)
    assert data["points"][-1]["actual_value"] == pytest.approx(250.972, abs=0.001)
    assert all(p["actual_value"] >= 0 for p in data["points"])


def test_actual_accepts_mixed_input_timezone_and_returns_wita(client):
    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2024-02-29T16:00:00Z",  # same instant as 2024-03-01 00:00 WITA
        "to": "2024-03-02T16:00:00Z",
    }, headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["from"].endswith("+08:00")
    assert data["to"].endswith("+08:00")
    assert len(data["points"]) == 3
    assert data["points"][0]["timestamp"] == "2024-03-01T00:00:00+08:00"


def test_actual_no_overlap_returns_empty_200_not_404(client):
    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2026-10-01T00:00:00+08:00",
        "to": "2026-10-07T00:00:00+08:00",
    }, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["points"] == []
    assert r.json()["n"] == 0


def test_actual_requires_api_key(client):
    r = client.get(URL, params=known_week())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E_UNAUTHORIZED"


def test_actual_unknown_site_404_site_unknown(client):
    r = client.get(URL, params={**known_week(), "site_id": "PLTS_UNKNOWN"}, headers=HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_SITE_UNKNOWN"


def test_actual_naive_range_rejected(client):
    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2024-03-01T00:00:00",
        "to": "2024-03-07T00:00:00+08:00",
    }, headers=HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_TZ_REQUIRED"


def test_actual_to_before_from_rejected(client):
    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2024-03-07T00:00:00+08:00",
        "to": "2024-03-01T00:00:00+08:00",
    }, headers=HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_TIME_RANGE_INVALID"


def test_actual_is_deterministic(client):
    a = client.get(URL, params=known_week(), headers=HEADERS)
    b = client.get(URL, params=known_week(), headers=HEADERS)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_actual_available_even_when_model_not_ready(client):
    """Historical data does not require a loaded forecasting model."""
    client.app.state.model_loaded = False
    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2024-03-01T00:00:00+08:00",
        "to": "2024-03-02T00:00:00+08:00",
    }, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert len(r.json()["points"]) == 2


def test_actual_response_schema_validates(client):
    from app.actual_schemas import ActualResponse

    r = client.get(URL, params={
        "site_id": "PLTS_KUPANG",
        "from": "2024-03-01T00:00:00+08:00",
        "to": "2024-03-03T00:00:00+08:00",
    }, headers=HEADERS)
    assert r.status_code == 200, r.text
    parsed = ActualResponse.model_validate(r.json())
    assert parsed.n == len(parsed.points) == 3
