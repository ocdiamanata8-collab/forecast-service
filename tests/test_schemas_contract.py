"""Test yang MENEGAKKAN skema Pydantic sebagai kontrak.

    pytest tests/test_schemas_contract.py -q

Dua arah pemeriksaan:

A. REQUEST — yang salah HARUS ditolak 422 dengan ``details`` yang jelas
   (kontrak §3.3), bukan lolos diam-diam atau 500.
B. RESPONSE — yang tidak sesuai kontrak HARUS tertangkap. Satu-satunya
   cara membuktikan skema benar-benar mengurung respons adalah dengan
   mencoba membangun ``ForecastResponse`` dari payload liar dan
   memastikan Pydantic menolaknya.

Filosofi: kalau test ini hanya memeriksa "response 200", skema bisa
rusak dan tetap lulus. Karena itu sebagian besar test di sini MEMANG
mengharapkan kegagalan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.main import create_app  # noqa: E402
from app.schemas import (  # noqa: E402
    ForecastPoint,
    ForecastRequest,
    ForecastResponse,
    SEMVER,
)
from app.version import model_version  # noqa: E402

DEMO_KEY = "dev-key-change-me-0123456789"
HEADERS = {"X-API-Key": DEMO_KEY}
SITE = "PLTS_KUPANG"
#: Versi diambil dari model/VERSION — literal dilarang (test_version.py
#: men-scan seluruh test untuk hardcode versi).
GOOD_VERSION = model_version()


# ---------------------------------------------------------------------------
# Pembantu payload
# ---------------------------------------------------------------------------
def valid_history(n: int = 40) -> list[dict[str, object]]:
    """40 poin harian valid, tz-aware, urut, langkah teratur."""
    base = datetime(2024, 1, 1, 0, 0)  # naive → kita tambahkan offset manual
    return [
        {
            "timestamp": f"{base.replace(day=1).year}-01-{i + 1:02d}T00:00:00+08:00",
            "value": 180.0,
        }
        for i in range(min(n, 31))
    ] + [
        {
            "timestamp": f"2024-02-{i - 31 + 1:02d}T00:00:00+08:00",
            "value": 185.0,
        }
        for i in range(31, n)
    ]


def valid_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "horizon": 7,
        "history": valid_history(),
    }
    body.update(overrides)
    return body


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def post(client: TestClient, body: dict[str, object]):
    return client.post("/api/v1/forecast", json=body, headers=HEADERS)


# ===========================================================================
# A. REQUEST — salah tipe harus DITOLAK
# ===========================================================================
def test_wrong_type_horizon_string_rejected(client):
    """Kontrak §3: "request salah tipe langsung ditolak".

    Regresi: Pydantic default MENGONVERSI "7" → 7 dan lolos 200 tanpa
    error — client yang salah tipe tidak pernah tahu. Angka harus angka.
    """
    r = post(client, valid_body(horizon="7"))
    assert r.status_code == 422, (
        f"horizon string harus 422, dapat {r.status_code}: {r.text[:300]}"
    )
    details = r.json()["error"]["details"]
    assert any(d["field"] == "horizon" for d in details), details


def test_wrong_type_horizon_float_rejected(client):
    """7.0 bukan 7 untuk kontrak integer — jangan diam-diam dibulatkan."""
    r = post(client, valid_body(horizon=7.5))
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert any(d["field"] == "horizon" for d in details), details


def test_wrong_type_history_value_string_rejected(client):
    """``value`` harus numerik — string yang bisa dinomalkan pun ditolak."""
    history = valid_history()
    history[0]["value"] = "180.0"
    r = post(client, valid_body(history=history))
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert any(d["field"].startswith("history") for d in details), details


def test_missing_required_field_rejected(client):
    r = post(client, {"horizon": 7})
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    assert "site_id" in fields and "history" in fields, fields


def test_extra_field_rejected(client):
    """StrictSchema: field asing → 422, bukan diabaikan diam-diam."""
    r = post(client, valid_body(coloumn_typo=1))
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    assert "coloumn_typo" in fields, fields


def test_site_id_pattern_rejected(client):
    r = post(client, valid_body(site_id="plts-kupang"))
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "site_id"


def test_history_too_short_rejected(client):
    r = post(client, valid_body(history=valid_history()[:10]))
    assert r.status_code == 422
    assert any(d["field"] == "history" for d in r.json()["error"]["details"])


def test_naive_timestamp_rejected_with_contract_code(client):
    """§1.5: timestamp tanpa offset → 422 + kode kontrak E_TZ_REQUIRED."""
    history = valid_history()
    history[0]["timestamp"] = "2024-01-01T00:00:00"
    r = post(client, valid_body(history=history))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_TZ_REQUIRED", r.json()["error"]


# ===========================================================================
# A2. KODE ERROR HARUS SPESIFIK (kontrak §6.1 — inilah yang diparse client)
# ===========================================================================
def test_horizon_out_of_range_uses_specific_code(client):
    """Kontrak §6.1: ``E_HORIZON_RANGE``, bukan ``E_VALIDATION_FAILED`` generik."""
    r = post(client, valid_body(horizon=31))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_HORIZON_RANGE", (
        f"dapat {r.json()['error']['code']}"
    )


def test_history_too_short_uses_specific_code(client):
    """Kontrak §6.1: ``E_HISTORY_TOO_SHORT``."""
    r = post(client, valid_body(history=valid_history()[:10]))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_HISTORY_TOO_SHORT", (
        f"dapat {r.json()['error']['code']}"
    )


def test_naive_timestamp_details_are_structured(client):
    """§3.3: tiap item ``{field, issue, value}`` — field bukan 'body'."""
    history = valid_history()
    history[0]["timestamp"] = "2024-01-01T00:00:00"
    r = post(client, valid_body(history=history))
    details = r.json()["error"]["details"]
    assert details, "422 wajib membawa details[]"
    for d in details:
        assert set(d) >= {"field", "issue"}, d
        assert d["field"], "field tidak boleh kosong"
        assert d["field"] != "body", f"field generik 'body' tidak informatif: {d}"


def test_error_envelope_always_has_request_id(client):
    """§3.3: ``request_id`` wajib dan sama dengan header X-Request-Id."""
    r = post(client, valid_body(horizon=99))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["request_id"] == r.headers["X-Request-Id"]
    assert set(err) >= {"code", "message", "request_id"}


# ===========================================================================
# B. RESPONSE — payload liar HARUS tertangkap oleh skema
# ===========================================================================
def test_schema_rejects_response_missing_required_field():
    """Bukti skema mengurung response: field wajib hilang → ValidationError."""
    payload = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "unit": "W/m2",
        "ts": "2024-01-31T00:00:00+08:00",
        "generated_at": "2024-01-31T06:00:00+08:00",
        "horizon": 1,
        "model_version": GOOD_VERSION,
        "order": "ARIMA(1, 0, 2)",
        # forecast HILANG
    }
    with pytest.raises(ValidationError) as exc:
        ForecastResponse.model_validate(payload)
    assert "forecast" in str(exc.value)


def test_schema_rejects_horizon_mismatch():
    """§C-03: len(forecast) == horizon. Skema menegakkannya sendiri."""
    payload = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "unit": "W/m2",
        "ts": "2024-01-31T00:00:00+08:00",
        "generated_at": "2024-01-31T06:00:00+08:00",
        "horizon": 3,  # tapi hanya 1 poin
        "model_version": GOOD_VERSION,
        "order": "ARIMA(1, 0, 2)",
        "forecast": [
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",
                "forecast_value": 200.0,
                "model_version": GOOD_VERSION,
            }
        ],
    }
    with pytest.raises(ValidationError) as exc:
        ForecastResponse.model_validate(payload)
    assert "horizon" in str(exc.value) or "forecast" in str(exc.value)


def test_schema_rejects_negative_forecast_value():
    """§C-05: forecast_value >= 0."""
    with pytest.raises(ValidationError):
        ForecastPoint.model_validate(
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",
                "forecast_value": -1.0,
                "model_version": GOOD_VERSION,
            }
        )


def test_schema_rejects_naive_forecast_time():
    """§C-01: forecast_time wajib tz-aware."""
    with pytest.raises(ValidationError):
        ForecastPoint.model_validate(
            {
                "forecast_time": "2024-02-01T00:00:00",
                "forecast_value": 200.0,
                "model_version": GOOD_VERSION,
            }
        )


def test_schema_rejects_wrong_unit():
    """§4: unit hanya W/m2 untuk target irradiance v1.0.0."""
    payload = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "unit": "kW",  # salah — kontrak memakai W/m2
        "ts": "2024-01-31T00:00:00+08:00",
        "generated_at": "2024-01-31T06:00:00+08:00",
        "horizon": 1,
        "model_version": GOOD_VERSION,
        "order": "ARIMA(1, 0, 2)",
        "forecast": [
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",
                "forecast_value": 200.0,
                "model_version": GOOD_VERSION,
            }
        ],
    }
    with pytest.raises(ValidationError):
        ForecastResponse.model_validate(payload)


def test_schema_rejects_non_semver_model_version():
    with pytest.raises(ValidationError):
        ForecastPoint.model_validate(
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",
                "forecast_value": 200.0,
                "model_version": "terbaru",
            }
        )


def test_schema_rejects_extra_response_field():
    """§1: response tidak boleh membawa field tak dikenal (extra=forbid)."""
    payload = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "unit": "W/m2",
        "ts": "2024-01-31T00:00:00+08:00",
        "generated_at": "2024-01-31T06:00:00+08:00",
        "horizon": 1,
        "model_version": GOOD_VERSION,
        "order": "ARIMA(1, 0, 2)",
        "forecast": [
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",
                "forecast_value": 200.0,
                "model_version": GOOD_VERSION,
            }
        ],
        "debug_leaked": True,  # tidak boleh lolos
    }
    with pytest.raises(ValidationError):
        ForecastResponse.model_validate(payload)


def test_schema_rejects_unsorted_forecast_times():
    """§C-04: forecast_time terurut naik & unik."""
    payload = {
        "site_id": SITE,
        "target": "irradiance",
        "resolution": "daily",
        "unit": "W/m2",
        "ts": "2024-01-31T00:00:00+08:00",
        "generated_at": "2024-01-31T06:00:00+08:00",
        "horizon": 2,
        "model_version": GOOD_VERSION,
        "order": "ARIMA(1, 0, 2)",
        "forecast": [
            {
                "forecast_time": "2024-02-02T00:00:00+08:00",
                "forecast_value": 200.0,
                "model_version": GOOD_VERSION,
            },
            {
                "forecast_time": "2024-02-01T00:00:00+08:00",  # mundur
                "forecast_value": 210.0,
                "model_version": GOOD_VERSION,
            },
        ],
    }
    with pytest.raises(ValidationError):
        ForecastResponse.model_validate(payload)


# ===========================================================================
# C. RESPONSE NYATA — mematuhi skema & kontrak §3.1
# ===========================================================================
def test_actual_response_matches_schema(client):
    """Respons sukses harus lolos validasi skema secara ulang — bukan cuma 200."""
    r = post(client, valid_body(horizon=7))
    assert r.status_code == 200, r.text[:400]
    body = r.json()

    # Validasi ulang terhadap skema: kalau handler mengirim sesuatu yang
    # di luar kontrak, ini gagal meski statusnya 200.
    parsed = ForecastResponse.model_validate(body)
    assert parsed.horizon == len(parsed.forecast)
    assert parsed.unit == "W/m2"
    assert parsed.timezone == "Asia/Makassar"
    assert parsed.site_id == SITE
    assert parsed.model_version == parsed.forecast[0].model_version

    # Field kontrak §3.1 ada, field sketsa yang salah tidak ada.
    assert "data" not in body, "kontrak memakai 'forecast', bukan 'data'"
    assert "forecast_kw" not in str(body), "kontrak memakai forecast_value, bukan forecast_kw"
    assert "ts" in body and "generated_at" in body
    assert parsed.ts.tzinfo is not None and parsed.generated_at.tzinfo is not None


def test_actual_points_match_point_schema(client):
    r = post(client, valid_body(horizon=3))
    assert r.status_code == 200, r.text[:400]
    for point in r.json()["forecast"]:
        parsed = ForecastPoint.model_validate(point)
        assert parsed.forecast_value >= 0
        assert str(parsed.forecast_time.tzinfo) == "Asia/Makassar"
        # Pembulatan §3.1: 3 desimal.
        assert round(parsed.forecast_value, 3) == parsed.forecast_value


def test_schema_constants_align_with_contract():
    """Semver skema == semver parser versi (dites silang)."""
    from app.version import SEMVER_PATTERN

    assert SEMVER == SEMVER_PATTERN.pattern


def test_horizon_bounds_match_contract():
    """§5.1: horizon 1..30 (bukan 1..168 — itu resolusi hourly)."""
    # Lewat 30 → ditolak
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(valid_body(horizon=31))
    # Lewat 1 → ditolak
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(valid_body(horizon=0))
    # 1 dan 30 → diterima
    ForecastRequest.model_validate(valid_body(horizon=1))
    ForecastRequest.model_validate(valid_body(horizon=30))


def test_history_requires_timestamps_not_bare_floats():
    """§1: history butuh timestamp per baris — list[float] tidak cukup.

    Tanpa timestamp engine tidak bisa menentukan resolusi; kontrak
    §1.5 melarang timestamp naive sehingga bare float ditolak total.
    """
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {"site_id": SITE, "horizon": 7, "history": [180.0] * 40}
        )
