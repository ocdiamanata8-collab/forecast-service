"""Uji API terhadap kontrak KONTRAK_API_v1.md (Fase 5).

Fokus: bentuk respons, kode error, auth, pagination, determinisme.
Run: pytest tests/test_api_contract.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import DEMO_KEY  # noqa: E402


def _history_payload(n: int = 60) -> list[dict[str, object]]:
    """History harian deterministik dengan offset eksplisit +08:00 (kontrak §1.5)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    doy = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * doy / 365.25), 0, None)
    return [
        {"timestamp": ts.isoformat(), "value": round(float(v), 3)}
        for ts, v in zip(idx, values)
    ]


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "site_id": "PLTS_KUPANG",
        "target": "irradiance",
        "resolution": "daily",
        "horizon": 7,
        "history": _history_payload(),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth (§1.1)
# ---------------------------------------------------------------------------
def test_missing_api_key_returns_401(client):
    r = client.post("/api/v1/forecast", json=_body())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E_UNAUTHORIZED"
    assert "X-Request-Id" in r.headers


def test_wrong_api_key_returns_403(client):
    r = client.post("/api/v1/forecast", json=_body(), headers={"X-API-Key": "wrong-key-0000000000"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E_FORBIDDEN"


def test_successful_auth_includes_rate_limit_headers(client):
    """§1.3: response yang lolos juga menunjukkan kuota per key."""
    r = client.get("/api/v1/model/info", headers={"X-API-Key": "dev-key-change-me-0123456789"})
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "60"
    assert 0 <= int(r.headers["X-RateLimit-Remaining"]) < 60


def test_consumer_keys_are_isolated_and_revocable(client, monkeypatch):
    """Satu konsumen punya key+kuota sendiri dan bisa dicabut tanpa mematikan
    konsumen lain (API_KEYS labels + RateLimiter per raw key).
    """
    import os

    from app.security import get_limiter, get_registry

    key_monitoring = "monitoring-key-0123456789abcdef"
    key_ci = "ci-consumer-key-0123456789abcdef"
    monkeypatch.setenv("API_KEYS", f"monitoring:{key_monitoring},ci:{key_ci}")
    registry = get_registry()
    saved_keys = dict(registry.keys)
    registry.keys.clear()
    limiter = get_limiter()
    saved_hits = dict(limiter._hits)
    limiter._hits.clear()
    try:
        # Lifespan parse env -> label registry; distinct labels.
        from fastapi.testclient import TestClient
        from app.main import create_app

        with TestClient(create_app()) as c:
            assert registry.keys[key_monitoring] == "monitoring"
            assert registry.keys[key_ci] == "ci"

            # Key A consumes its own 60/min; key B remains independent.
            h_monitoring = {"X-API-Key": key_monitoring}
            h_ci = {"X-API-Key": key_ci}
            for _ in range(60):
                ok = c.get("/api/v1/model/info", headers=h_monitoring)
                assert ok.status_code == 200
            limited = c.get("/api/v1/model/info", headers=h_monitoring)
            assert limited.status_code == 429
            assert limited.json()["error"]["code"] == "E_RATE_LIMITED"
            assert c.get("/api/v1/model/info", headers=h_ci).status_code == 200

            # Revoke only monitoring: it gets 403, CI is still valid.
            registry.keys.pop(key_monitoring)
            revoked = c.get("/api/v1/model/info", headers=h_monitoring)
            assert revoked.status_code == 403
            assert revoked.json()["error"]["code"] == "E_FORBIDDEN"
            assert c.get("/api/v1/model/info", headers=h_ci).status_code == 200
    finally:
        registry.keys.clear()
        registry.keys.update(saved_keys)
        limiter._hits.clear()
        limiter._hits.update(saved_hits)


def test_health_is_open_without_key(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded", "unavailable"}
    assert "model_version" not in body  # §5.6: info model tidak di health


# ---------------------------------------------------------------------------
# POST /forecast (§5.1)
# ---------------------------------------------------------------------------
def test_post_forecast_shape(client, auth_headers):
    r = client.post("/api/v1/forecast", json=_body(horizon=5), headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["site_id"] == "PLTS_KUPANG"
    assert body["target"] == "irradiance"
    assert body["unit"] == "W/m2"
    assert body["timezone"] == "Asia/Makassar"
    assert body["horizon"] == 5
    assert len(body["forecast"]) == 5
    assert body["model_version"].startswith("v")
    # C-01/C-02: setiap timestamp membawa offset eksplisit zona site.
    for point in body["forecast"]:
        assert point["forecast_time"].endswith("+08:00")
        assert point["forecast_value"] >= 0
        assert point["model_version"] == body["model_version"]
    assert body["generated_at"].endswith("+08:00")
    assert body["ts"].endswith("+08:00")
    # Kontrak §1/§3.2: forecast_id tidak ada pada POST on-demand —
    # bukan `null`. Field opsional dihilangkan, bukan dikirim kosong.
    assert "forecast_id" not in body


def test_post_forecast_is_deterministic(client, auth_headers):
    body = _body(horizon=7)
    a = client.post("/api/v1/forecast", json=body, headers=auth_headers)
    b = client.post("/api/v1/forecast", json=body, headers=auth_headers)
    assert a.status_code == b.status_code == 200
    assert a.json()["forecast"] == b.json()["forecast"]  # kontrak §5.1


def test_post_forecast_naive_timestamp_rejected(client, auth_headers):
    body = _body(history=[{"timestamp": "2024-01-01T00:00:00", "value": 100.0}] * 40)
    r = client.post("/api/v1/forecast", json=body, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E_TZ_REQUIRED"


def test_post_forecast_history_too_short(client, auth_headers):
    body = _body(history=_history_payload(10))
    r = client.post("/api/v1/forecast", json=body, headers=auth_headers)
    assert r.status_code == 422
    # Kontrak §6.1: kode spesifik — client memparse `code`, bukan `message`.
    assert r.json()["error"]["code"] == "E_HISTORY_TOO_SHORT"


def test_post_forecast_horizon_out_of_range(client, auth_headers):
    r = client.post("/api/v1/forecast", json=_body(horizon=31), headers=auth_headers)
    assert r.status_code == 422
    # Kontrak §6.1 menetapkan E_HORIZON_RANGE (bukan generik E_VALIDATION_FAILED).
    assert r.json()["error"]["code"] == "E_HORIZON_RANGE"


def test_post_forecast_accepts_utc_input_offsets(client, auth_headers):
    """Input `Z` (SCADA/ERA5) diterima; respons tetap +08:00 (§1.5)."""
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * idx.dayofyear / 365.25), 0, None)
    history = [
        {"timestamp": ts.isoformat().replace("+00:00", "Z"), "value": float(v)}
        for ts, v in zip(idx, values)
    ]
    r = client.post(
        "/api/v1/forecast",
        json=_body(history=history, horizon=3),
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["forecast"][0]["forecast_time"].endswith("+08:00")


# ---------------------------------------------------------------------------
# GET /forecast/latest (§5.2)
# ---------------------------------------------------------------------------
def test_latest_without_forecast_returns_404(client, auth_headers):
    r = client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_KUPANG"},
                   headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_NO_FORECAST"


def test_latest_after_storage_roundtrip(client, auth_headers):
    """Simpan lewat store, lalu dibaca endpoint latest (kontrak §8)."""
    from datetime import datetime

    from app import preprocessing as pp
    from app.storage import InMemoryForecastStore, StoredForecast, items_from_response
    from app.api.forecast import run_forecast
    from app.schemas import ForecastRequest

    req = ForecastRequest(**_body(horizon=7))
    resp = run_forecast(req)
    store = InMemoryForecastStore()
    store.save(
        items_from_response(
            # §3.2: forecast_id = UUID — string sembarangan ditolak
            # penyedia DB (kolom uuid), sama seperti test_error_handling.
            forecast_id="00000000-0000-4000-8000-000000000001",
            site_id=resp.site_id,
            target=resp.target,
            resolution=resp.resolution,
            unit=resp.unit,
            ts=resp.ts,
            generated_at=resp.generated_at,
            model_version=resp.model_version,
            points=resp.forecast,
        )
    )
    client.app.state.store = store
    r = client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_KUPANG"},
                   headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["forecast_id"] == "00000000-0000-4000-8000-000000000001"
    assert len(r.json()["forecast"]) == 7


# ---------------------------------------------------------------------------
# GET /forecast/history + pagination (§5.3)
# ---------------------------------------------------------------------------
def _seed_store(store, n: int = 25) -> None:
    from datetime import datetime, timedelta, timezone

    from app.storage import StoredForecast, items_from_response
    from app.schemas import ForecastPoint

    from app.schemas import ForecastPoint
    from app.version import model_version as current_version

    base = datetime(2026, 10, 1, 6, 0, tzinfo=timezone(timedelta(hours=8)))
    for i in range(n):
        gen = base + timedelta(hours=i)
        point = ForecastPoint(
            forecast_time=base + timedelta(days=1),
            forecast_value=212.5,
            model_version=current_version(),
        )
        store.save(
            items_from_response(
                # §3.2: forecast_id = UUID — kolom DB bertipe uuid menolak
                # "id-000". Deterministik per i (dedupe paginasi tetap jalan).
                forecast_id=f"00000000-0000-4000-8000-{i:012d}",
                site_id="PLTS_KUPANG",
                target="irradiance",
                resolution="daily",
                unit="W/m2",
                ts=base - timedelta(days=1),
                generated_at=gen,
                model_version=current_version(),
                points=[point],
            )
        )


def test_history_pagination_cursor(client, auth_headers):
    from app.storage import InMemoryForecastStore

    store = InMemoryForecastStore()
    _seed_store(store, n=25)
    client.app.state.store = store

    first = client.get("/api/v1/forecast/history",
                       params={"site_id": "PLTS_KUPANG", "limit": 10},
                       headers=auth_headers)
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 10
    assert body["page"]["has_more"] is True
    cursor = body["page"]["next_cursor"]
    assert cursor  # opaque, ikut kontrak

    second = client.get("/api/v1/forecast/history",
                        params={"site_id": "PLTS_KUPANG", "limit": 10, "cursor": cursor},
                        headers=auth_headers)
    assert second.status_code == 200
    page2 = second.json()["items"]
    assert len(page2) == 10

    # Dedupe berdasarkan forecast_id (kontrak §5.3-4).
    ids1 = {i["forecast_id"] for i in body["items"]}
    ids2 = {i["forecast_id"] for i in page2}
    assert ids1 & ids2 == set()

    # Urutan generated_at descending (kontrak §5.3-3).
    gens = [i["generated_at"] for i in body["items"]]
    assert gens == sorted(gens, reverse=True)


def test_history_invalid_cursor(client, auth_headers):
    r = client.get("/api/v1/forecast/history",
                   params={"site_id": "PLTS_KUPANG", "cursor": "not-a-cursor"},
                   headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_CURSOR_INVALID"


def test_history_limit_out_of_range(client, auth_headers):
    r = client.get("/api/v1/forecast/history",
                   params={"site_id": "PLTS_KUPANG", "limit": 500},
                   headers=auth_headers)
    assert r.status_code == 422
    # §6.1: kode spesifik untuk client monitoring, bukan generik.
    assert r.json()["error"]["code"] == "E_LIMIT_RANGE"


# ---------------------------------------------------------------------------
# Model info + performance (§5.4–5.5)
# ---------------------------------------------------------------------------
def test_model_info_shape(client, auth_headers):
    r = client.get("/api/v1/model/info", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["model_version"].startswith("v")
    assert body["algorithm"] == "ARIMA"
    assert body["order"] == [1, 0, 2]
    assert body["timezone"] == "Asia/Makassar"
    assert body["deterministic"] is True
    assert body["evaluation_status"] in {"GO", "NO_GO", "UNKNOWN"}


def test_performance_without_evaluation_is_unknown(client, auth_headers, tmp_path):
    """Tanpa laporan di cache maupun disk -> 200 UNKNOWN (kontrak §5.5)."""
    from app.api import model as model_api

    original = model_api._EVAL_DIR
    model_api._EVAL_DIR = tmp_path  # folder kosong: report.json tidak ada
    try:
        r = client.get("/api/v1/model/performance", params={"site_id": "PLTS_KUPANG"},
                       headers=auth_headers)
        assert r.status_code == 200  # kontrak: 200, bukan 404
        body = r.json()
        assert body["status"] == "UNKNOWN"
        assert body["horizons"] == []
        # UNKNOWN memakai ambang bawaan — kini 28.0 (disesuaikan 2026-10-14).
        assert body["thresholds"]["max_mape"] == 28.0
    finally:
        model_api._EVAL_DIR = original


def test_performance_reads_report_json(client, auth_headers, tmp_path):
    """Laporan dari `scripts/run_evaluation.py` dipetakan ke kontrak §5.5."""
    import json as jsonlib

    from app.api import model as model_api

    payload = {
        "generated_at": "2026-10-02T00:00:00",
        "n_rows": 1096,
        "n_origins": 336,
        "holdout": 365,
        "holdout_start": "2025-01-01T00:00:00+08:00",
        "holdout_end": "2025-12-31T00:00:00+08:00",
        "order": [1, 0, 2],
        "thresholds": {
            "max_mae": 45.0, "max_mape": 28.0, "max_mape_long": 33.0,
            "min_skill": 0.0, "primary_horizon": 7,
        },
        "status": "NO_GO",
        "failures": ["MAPE horizon 7 = 25.56% > 28.00%"],
        "blocks": [
            {"horizon": 7, "mae": 40.255, "rmse": 50.022, "mape": 25.56,
             "skill": 0.244, "baseline_mae": 53.222, "baseline_mape": 0.0, "n": 336},
        ],
    }
    (tmp_path / "report.json").write_text(jsonlib.dumps(payload), encoding="utf-8")

    original = model_api._EVAL_DIR
    model_api._EVAL_DIR = tmp_path
    try:
        r = client.get("/api/v1/model/performance", params={"site_id": "PLTS_KUPANG"},
                       headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "NO_GO"
        assert body["failures"] == ["MAPE horizon 7 = 25.56% > 28.00%"]
        assert body["n_samples"] == 336
        assert body["thresholds"]["max_mape"] == 28.0
        assert body["horizons"][0]["mape"] == 25.56
        assert body["horizons"][0]["skill"] == 0.244
        # Konteks telusur: data_rows = n_rows laporan (1096), BUKAN n_samples.
        assert body["data_rows"] == 1096
        # report.json lama menyimpan generated_at naive; parser melokalisasi
        # eksplisit ke zona site sehingga respons selalu membawa offset
        # (kontrak §1.5 — timestamp tanpa offset melanggar kontrak).
        assert body["evaluated_at"].endswith("+08:00"), body["evaluated_at"]
    finally:
        model_api._EVAL_DIR = original


def test_performance_with_cached_report(client, auth_headers):
    """Sisipkan laporan evaluasi; endpoint memetakannya ke kontrak §5.5."""
    from app import evaluation as ev

    data = pd.DataFrame(
        {"irradiance": np.clip(
            180 + 60 * np.sin(2 * np.pi * np.arange(200) / 365.25), 0, None)},
        index=pd.date_range("2024-01-01", periods=200, freq="D", tz="Asia/Makassar"),
    )
    report = ev.rolling_backtest(
        data, target="irradiance", horizons=(1, 7, 14), holdout=60, stride=9
    )
    client.app.state.performance_cache = {("PLTS_KUPANG", "daily"): report}

    r = client.get("/api/v1/model/performance", params={"site_id": "PLTS_KUPANG"},
                   headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"GO", "NO_GO"}
    assert len(body["horizons"]) == 3
    assert {h["horizon"] for h in body["horizons"]} == {1, 7, 14}
    assert isinstance(body["failures"], list)
    assert body["n_samples"] == report.n_origins


# ---------------------------------------------------------------------------
# Determinisme lintas proses & envelope error (§3.3)
# ---------------------------------------------------------------------------
def test_error_envelope_shape(client, auth_headers):
    r = client.post("/api/v1/forecast", json=_body(), headers={"X-API-Key": "x"})
    assert r.status_code == 403
    err = r.json()["error"]
    assert set(err) >= {"code", "message", "request_id"}
    assert r.headers["X-Request-Id"] == err["request_id"]


def test_unknown_path_returns_envelope(client):
    r = client.get("/api/v1/tidak-ada")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_NOT_FOUND"


def test_rate_limit_kicks_in(client, auth_headers):
    """§1.3 — 60 req/menit per key; request ke-61 harus 429."""
    status_codes = []
    for _ in range(61):
        r = client.get("/api/v1/model/info", headers=auth_headers)
        status_codes.append(r.status_code)
        if r.status_code == 429:
            assert r.headers.get("Retry-After")
            assert r.json()["error"]["code"] == "E_RATE_LIMITED"
            break
    assert 429 in status_codes, "rate limit tidak aktif setelah 60 request"
