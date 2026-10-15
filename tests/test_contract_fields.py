"""Pengaman kontrak API: setiap endpoint divalidasi FIELD-PER-FIELD.

    pytest tests/test_contract_fields.py -q

Prinsip (instruksi): test ini adalah pengaman utama — kalau suatu hari nama
field berubah, integrasi GAGAL DI CI, bukan di hadapan pengguna/monitoring.

Dua lapis pengaman, keduanya sengaja:

1. **Tabel eksplisit** ``CONTRACT_FIELDS`` — daftar nama field SUKSES per
   endpoint, ditulis dari ``KONTRAK_API_v1.md`` §5.1–§5.7 (BUKAN disalin
   dari skema Pydantic). Kalau skema dan endpoint berubah bersama, Pydantic
   saja akan ikut "lulus" — tabel inilah yang menolak perubahan diam-diam.
   Menambah/menghapus/mengubah nama field = breaking change (§9.1): wajib
   edit kontrak + tabel ini bersamaan.

2. **Validasi ulang ``model_validate``** — payload JSON aktual diuji lagi
   terhadap skema Pydantic (tipe, validator timestamp, larangan extra).
   Menangkap kasus handler mengirim payload yang lolos response_model
   tapi tidak lolos skema saat dikonsumsi ulang.

Konteks repo ini (bukan sketsa generik):
- site ``PLTS_KUPANG`` (bukan PLTS001), zona ``+08:00`` WITA (bukan +07:00),
- nama field ``forecast_value`` / ``actual_value`` (bukan forecast_kw),
- daftar poin namanya ``forecast`` (bukan ``data``).

Field opsional mengikuti aturan §1: boleh HILANG dari JSON (bukan ``null``).
``POST /forecast`` memakai ``response_model_exclude_none`` — ``forecast_id``
tidak dikirim (§5.1); ``GET /forecast/latest`` selalu membawanya (§5.2).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler  # noqa: E402
from app.actual_schemas import ActualResponse  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import (  # noqa: E402
    ErrorEnvelope,
    ForecastHistoryResponse,
    ForecastResponse,
    HealthResponse,
    ModelInfoResponse,
    PerformanceResponse,
    SEMVER,
)
from app.storage import InMemoryForecastStore  # noqa: E402
from app.version import model_version  # noqa: E402

SITE = "PLTS_KUPANG"
HORIZON = 7

# ---------------------------------------------------------------------------
# Tabel kontrak — sumber: KONTRAK_API_v1.md §5.1–§5.7.
# HANYA field yang selalu terkirim. Field opsional yang boleh hilang dari JSON
# (§1 "null vs hilang") dicatat terpisah di OPTIONAL_FIELDS.
# ---------------------------------------------------------------------------
CONTRACT_FIELDS: dict[str, set[str]] = {
    "health": {
        "status", "service_version", "timestamp", "uptime_seconds", "model_loaded",
    },
    "model_info": {
        "model_version", "target", "resolution", "unit", "algorithm", "order",
        "timezone", "history_min_rows", "horizon_max", "training_rows",
        "trained_at", "training_range", "features", "exog_supported",
        "deterministic", "evaluation_status", "engine", "preprocessing",
    },
    "performance": {
        "site_id", "target", "resolution", "unit", "model_version",
        "evaluated_at", "data_rows", "evaluation_range", "n_samples",
        "thresholds", "status", "failures", "horizons",
    },
    "forecast_post": {
        "site_id", "target", "resolution", "unit", "timezone", "ts",
        "generated_at", "horizon", "model_version", "order", "forecast",
        # forecast_id HILANG di POST (exclude_none, §5.1) — lihat test sendiri.
    },
    "forecast_latest": {
        "site_id", "target", "resolution", "unit", "timezone", "ts",
        "generated_at", "horizon", "model_version", "order", "forecast",
        "forecast_id",
    },
    "history_page": {"items", "page"},
    "history_item": {
        "forecast_id", "site_id", "target", "resolution", "unit", "ts",
        "generated_at", "horizon", "model_version",
        # 'forecast' HILANG tanpa include_points (exclude_none, §5.3).
    },
    "history_page_info": {"limit", "has_more"},  # next_cursor opsional
    "actual": {
        "site_id", "target", "unit", "timezone", "from", "to", "source",
        "points", "n",
    },
    "forecast_point": {"forecast_time", "forecast_value", "model_version"},
    "actual_point": {"timestamp", "actual_value"},
    "thresholds": {
        "max_mae", "max_mape", "max_mape_long", "min_skill", "primary_horizon",
    },
    "horizon_metrics": {"horizon", "mae", "rmse", "mape", "skill"},
    "error_envelope": {"code", "message", "request_id"},  # details opsional
}

#: Field yang BOLEH hilang dari JSON (kontrak §1: hilang ≠ null).
OPTIONAL_FIELDS: dict[str, set[str]] = {
    "forecast_post": {"forecast_id"},
    "history_item": {"forecast"},
    "history_page_info": {"next_cursor"},
    "error_envelope": {"details"},
}


def assert_fields(payload: dict, key: str, label: str) -> None:
    """Payload punya TEPAT field kontrak — kelebihan maupun kekurangan = gagal."""
    expected = CONTRACT_FIELDS[key]
    optional = OPTIONAL_FIELDS.get(key, set())
    actual = set(payload)
    missing = expected - actual
    extra = actual - expected - optional
    assert not missing, f"{label}: field kontrak hilang {sorted(missing)}"
    assert not extra, (
        f"{label}: field TIDAK dikenal di kontrak {sorted(extra)} — "
        "ubah kontrak §9.1 (breaking) + tabel CONTRACT_FIELDS bersamaan"
    )


def assert_wita(value: str, label: str) -> None:
    """§1.5/§3.1: timestamp respons selalu membawa offset eksplisit +08:00."""
    assert value.endswith("+08:00"), f"{label} tanpa offset WITA: {value!r}"


def deterministic_history(n: int = 60) -> list[tuple[datetime, float]]:
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    return [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]


HEADERS = {"X-API-Key": "dev-key-change-me-0123456789"}
POST_BODY = {
    "site_id": SITE,
    "target": "irradiance",
    "resolution": "daily",
    "horizon": 5,
    "history": [
        {"timestamp": ts.isoformat(), "value": v}
        for ts, v in deterministic_history()
    ],
}


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def seeded_client(client):
    """Client dengan store berisi satu forecast hasil rantai scheduler NYATA."""
    store = InMemoryForecastStore()
    scheduler.run_and_store_forecast(
        store, site_id=SITE, history=deterministic_history(), horizon=HORIZON
    )
    client.app.state.store = store
    return client


def auth(client: TestClient, r):
    """Request-id wajib di SEMUA respons (§1.3) — sekalian diperiksa di sini."""
    assert "X-Request-Id" in r.headers, f"{client} tanpa X-Request-Id"
    return r


# ===========================================================================
# 1. GET /api/v1/health — §5.6, satu-satunya endpoint tanpa key
# ===========================================================================
def test_health_contract_fields(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    auth(client, r)
    body = r.json()
    assert_fields(body, "health", "GET /health")

    parsed = HealthResponse.model_validate(body)
    assert parsed.status in {"ok", "degraded", "unavailable"}
    assert parsed.service_version.startswith("v")
    assert parsed.uptime_seconds >= 0
    assert parsed.model_loaded is True
    assert_wita(body["timestamp"], "health.timestamp")
    # §1.1: info model TIDAK bocor ke health publik.
    assert "model_version" not in body, "model_version bocor ke /health"
    assert "evaluation_status" not in body, "metadata model bocor ke /health"


# ===========================================================================
# 2. GET /api/v1/model/info — §5.4
# ===========================================================================
def test_model_info_contract_fields(client):
    r = auth(client, client.get("/api/v1/model/info", headers=HEADERS))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_fields(body, "model_info", "GET /model/info")

    parsed = ModelInfoResponse.model_validate(body)
    assert body["model_version"] == model_version()
    assert body["target"] == "irradiance"
    assert body["resolution"] == "daily"
    assert body["unit"] == "W/m2"  # §4: satuan irradiance
    assert body["algorithm"] == "ARIMA"
    assert body["order"] == [1, 0, 2]
    assert body["timezone"] == "Asia/Makassar"
    assert body["history_min_rows"] >= 30
    assert body["horizon_max"] == 30
    assert body["training_rows"] > 0
    assert body["features"] == ["temperature", "tcc"]  # urutan training
    assert body["exog_supported"] is True
    assert body["deterministic"] is True
    assert body["evaluation_status"] in {"GO", "NO_GO", "UNKNOWN"}
    assert body["engine"] and body["preprocessing"]

    # Nested: trained_at + training_range{from,to} — semua tz-aware.
    assert_wita(body["trained_at"], "model_info.trained_at")
    assert set(body["training_range"]) == {"from", "to"}
    assert_wita(body["training_range"]["from"], "training_range.from")
    assert_wita(body["training_range"]["to"], "training_range.to")


# ===========================================================================
# 3. POST /api/v1/forecast — §5.1
# ===========================================================================
def test_post_forecast_contract_fields(client):
    r = auth(client, client.post("/api/v1/forecast", json=POST_BODY, headers=HEADERS))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_fields(body, "forecast_post", "POST /forecast")

    parsed = ForecastResponse.model_validate(body)
    assert parsed.horizon == len(parsed.forecast) == 5
    assert parsed.unit == "W/m2"
    assert parsed.timezone == "Asia/Makassar"
    assert parsed.site_id == SITE
    assert parsed.target == "irradiance"
    assert parsed.resolution == "daily"
    assert body["model_version"] == model_version()
    assert parsed.order == "ARIMA(1, 0, 2)"

    # §1: field opsional HILANG, bukan null.
    assert "forecast_id" not in body, "POST tidak menyimpan — forecast_id harus hilang (§5.1)"
    assert all(v is not None for v in body.values()), "null terkirim — kontrak §1: hilang ≠ null"

    assert_wita(body["ts"], "POST.ts")
    assert_wita(body["generated_at"], "POST.generated_at")
    for i, point in enumerate(body["forecast"]):
        assert_fields(point, "forecast_point", f"POST.forecast[{i}]")
        assert_wita(point["forecast_time"], f"POST.forecast[{i}].forecast_time")
        assert point["forecast_value"] >= 0, "C-05: nilai >= 0"
        assert round(point["forecast_value"], 3) == point["forecast_value"], \
            "§3.1: pembulatan 3 desimal"
        assert point["model_version"] == body["model_version"]


# ===========================================================================
# 4. GET /api/v1/forecast/latest — §5.2
# ===========================================================================
def test_latest_contract_fields(seeded_client):
    r = auth(seeded_client, seeded_client.get(
        "/api/v1/forecast/latest", params={"site_id": SITE}, headers=HEADERS
    ))
    assert r.status_code == 200, r.text  # bukan 404 E_NO_FORECAST (scheduler)
    body = r.json()
    assert_fields(body, "forecast_latest", "GET /latest")

    parsed = ForecastResponse.model_validate(body)
    assert parsed.horizon == len(parsed.forecast) == HORIZON
    assert body["forecast_id"], "latest wajib membawa forecast_id (§5.2)"
    assert body["unit"] == "W/m2"
    assert body["model_version"] == model_version()
    assert_wita(body["generated_at"], "latest.generated_at")
    assert_wita(body["ts"], "latest.ts")
    for i, point in enumerate(body["forecast"]):
        assert_fields(point, "forecast_point", f"latest.forecast[{i}]")
        assert_wita(point["forecast_time"], f"latest.forecast[{i}].forecast_time")
        assert point["forecast_value"] >= 0


def test_latest_ignores_unknown_query_horizon(seeded_client):
    """§5.2: `latest` membaca store apa adanya — TIDAK punya parameter horizon.

    URL monitoring lama bisa membawa `?horizon=24`; parameter tak dikenal
    diabaikan (pola HTTP), bukan 422 — dan horizon response = horizon
    tersimpan (scheduler: 7), bukan nilai query. Forecast on-demand dengan
    horizon pilihan tetap lewat POST /forecast (§5.1).
    """
    base = auth(seeded_client, seeded_client.get(
        "/api/v1/forecast/latest", params={"site_id": SITE}, headers=HEADERS
    ))
    with_query = auth(seeded_client, seeded_client.get(
        "/api/v1/forecast/latest",
        params={"site_id": SITE, "horizon": 24, "unknown_param": "x"},
        headers=HEADERS,
    ))
    assert base.status_code == with_query.status_code == 200
    # Response identik — query diabaikan, bukan memengaruhi isi.
    assert with_query.json() == base.json()
    assert with_query.json()["horizon"] == HORIZON == len(with_query.json()["forecast"])


# ===========================================================================
# 5. GET /api/v1/forecast/history — §5.3 (dua varian: default & include_points)
# ===========================================================================
def test_history_contract_fields_default(seeded_client):
    r = auth(seeded_client, seeded_client.get(
        "/api/v1/forecast/history", params={"site_id": SITE}, headers=HEADERS
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_fields(body, "history_page", "GET /history")
    ForecastHistoryResponse.model_validate(body)  # validasi ulang penuh

    assert isinstance(body["items"], list) and body["items"], "seed hilang"
    for i, item in enumerate(body["items"]):
        assert_fields(item, "history_item", f"history.items[{i}]")
        # Tanpa include_points: 'forecast' HILANG, bukan null (§5.3).
        assert "forecast" not in item, "default: poin tidak dikirim (bukan null)"
        assert_wita(item["generated_at"], f"items[{i}].generated_at")
        assert_wita(item["ts"], f"items[{i}].ts")
        assert item["horizon"] == HORIZON

    assert_fields(body["page"], "history_page_info", "history.page")
    assert body["page"]["limit"] >= 1
    assert isinstance(body["page"]["has_more"], bool)


def test_history_contract_fields_include_points(seeded_client):
    r = auth(seeded_client, seeded_client.get(
        "/api/v1/forecast/history",
        params={"site_id": SITE, "include_points": "true"}, headers=HEADERS,
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    ForecastHistoryResponse.model_validate(body)
    for item in body["items"]:
        assert "forecast" in item, "include_points=true wajib menyertakan poin"
        assert len(item["forecast"]) == item["horizon"]
        for point in item["forecast"]:
            assert_fields(point, "forecast_point", "history item point")
            assert_wita(point["forecast_time"], "history point.forecast_time")


# ===========================================================================
# 5b. Filter query §5.3 — REGRESI: alias Pydantic via Depends() tidak bekerja;
#     FastAPI mencocokkan nama field, bukan alias, sehingga `?from=` dulu
#     DIABAIKAN diam-diam (filter monitoring tidak pernah diterapkan).
#     Route kini memakai Query(alias="from") eksplisit — test ini mengunci
#     bahwa parameter benar-benar SAMPAI ke store, bukan sekadar 200 OK.
# ===========================================================================
@pytest.fixture()
def history_client(client):
    """Store dengan 3 forecast pada generated_at berbeda (untuk uji filter)."""
    from datetime import datetime as dt

    store = InMemoryForecastStore()
    hist = deterministic_history()
    for day, hour in ((1, 6), (2, 6), (3, 6)):
        gen = dt(2026, 10, day, hour, 0, tzinfo=ZoneInfo("Asia/Makassar"))
        scheduler.run_and_store_forecast(
            store, site_id=SITE, history=hist, horizon=HORIZON, generated_at=gen
        )
    client.app.state.store = store
    return client


def test_history_from_filter_applies_to_generated_at(history_client):
    """§5.3: `from` menyaring generated_at >= from — bukan diabaikan.

    REGRESI: sebelum perbaikan, `?from=` tidak pernah terisi (alias
    Pydantic + Depends) sehingga endpoint selalu mengembalikan SEMUA item.
    """
    r = auth(history_client, history_client.get(
        "/api/v1/forecast/history",
        params={"site_id": SITE, "from": "2026-10-02T00:00:00+08:00"},
        headers=HEADERS,
    ))
    assert r.status_code == 200, r.text
    gens = [i["generated_at"] for i in r.json()["items"]]
    assert gens, "from=2026-10-02 harus menyisakan >= 1 item"
    assert all(g >= "2026-10-02T00:00:00+08:00" for g in gens), \
        f"from tidak diterapkan — item di luar rentang bocor: {gens}"
    assert len(gens) < 3, f"from harus membuang entri sebelumnya, dapat {len(gens)}/3"


def test_history_to_filter_applies_to_generated_at(history_client):
    r = auth(history_client, history_client.get(
        "/api/v1/forecast/history",
        params={"site_id": SITE, "to": "2026-10-02T23:59:59+08:00"},
        headers=HEADERS,
    ))
    assert r.status_code == 200, r.text
    gens = [i["generated_at"] for i in r.json()["items"]]
    assert gens and all(g <= "2026-10-02T23:59:59+08:00" for g in gens), \
        f"to tidak diterapkan: {gens}"
    assert len(gens) == 2, f"to=2026-10-02 harus menyisakan 2 entri, dapat {gens}"


def test_history_to_before_from_returns_time_range_invalid(history_client):
    """§5.3/§6.1: `to < from` → 422 E_TIME_RANGE_INVALID (bukan 200 kosong)."""
    r = auth(history_client, history_client.get(
        "/api/v1/forecast/history",
        params={"site_id": SITE,
                "from": "2026-10-05T00:00:00+08:00",
                "to": "2026-10-01T00:00:00+08:00"},
        headers=HEADERS,
    ))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "E_TIME_RANGE_INVALID"


def test_history_from_naive_returns_tz_required(history_client):
    """§1.5/§6.1: filter tanpa offset → 422 E_TZ_REQUIRED (zona tidak ditebak)."""
    r = auth(history_client, history_client.get(
        "/api/v1/forecast/history",
        params={"site_id": SITE, "from": "2026-10-01T00:00:00"},
        headers=HEADERS,
    ))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "E_TZ_REQUIRED"


def test_history_limit_range_uses_specific_code(history_client):
    """§6.1: limit di luar 1..100 → 422 E_LIMIT_RANGE (bukan generik)."""
    for bad in (0, 101, 700):
        r = auth(history_client, history_client.get(
            "/api/v1/forecast/history",
            params={"site_id": SITE, "limit": bad}, headers=HEADERS,
        ))
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "E_LIMIT_RANGE", \
            f"limit={bad} -> {r.json()['error']['code']}"
    # Batas sah tetap diterima.
    for good in (1, 100):
        r = auth(history_client, history_client.get(
            "/api/v1/forecast/history",
            params={"site_id": SITE, "limit": good}, headers=HEADERS,
        ))
        assert r.status_code == 200, (good, r.text)
        assert r.json()["page"]["limit"] == good


def test_monitoring_trio_available_together(history_client):
    """Trio monitoring (§1 tabel endpoint): latest + history + performance
    semuanya 200 dari state yang sama — inilah yang dipanggil platform."""
    for path, params in (
        ("/api/v1/forecast/latest", {"site_id": SITE}),
        ("/api/v1/forecast/history", {"site_id": SITE, "limit": 5}),
        ("/api/v1/model/performance", {"site_id": SITE}),
    ):
        r = auth(history_client, history_client.get(path, params=params, headers=HEADERS))
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"


# ===========================================================================
# 6. GET /api/v1/forecast/actual — §5.7
# ===========================================================================
def test_actual_contract_fields(client):
    r = auth(client, client.get("/api/v1/forecast/actual", params={
        "site_id": SITE,
        "from": "2024-03-01T00:00:00+08:00",
        "to": "2024-03-07T00:00:00+08:00",
    }, headers=HEADERS))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_fields(body, "actual", "GET /actual")

    parsed = ActualResponse.model_validate(body)
    assert body["target"] == "irradiance"
    assert body["unit"] == "W/m2"
    assert body["timezone"] == "Asia/Makassar"
    assert body["source"] == "data/actuals.csv"  # tanpa path filesystem
    assert body["n"] == len(body["points"]) == 7
    assert_wita(body["from"], "actual.from")
    assert_wita(body["to"], "actual.to")
    for i, point in enumerate(body["points"]):
        assert_fields(point, "actual_point", f"actual.points[{i}]")
        assert_wita(point["timestamp"], f"actual.points[{i}].timestamp")
        assert point["actual_value"] >= 0
    assert parsed.n == 7


# ===========================================================================
# 6b. GET /api/v1/model/performance — §5.5 (laporan report.json nyata)
# ===========================================================================
def test_performance_contract_fields(client):
    r = auth(client, client.get("/api/v1/model/performance",
                                params={"site_id": SITE}, headers=HEADERS))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_fields(body, "performance", "GET /model/performance")

    PerformanceResponse.model_validate(body)
    assert body["site_id"] == SITE
    assert body["target"] == "irradiance"
    assert body["unit"] == "W/m2"
    assert body["model_version"] == model_version()
    assert body["status"] in {"GO", "NO_GO", "UNKNOWN"}
    assert isinstance(body["failures"], list)
    assert isinstance(body["horizons"], list)
    assert body["data_rows"] >= 0 and body["n_samples"] >= 0

    # Nested thresholds & horizons[] — field penuh, tanpa kejutan.
    assert_fields(body["thresholds"], "thresholds", "performance.thresholds")
    assert body["thresholds"]["primary_horizon"] == 7
    for i, h in enumerate(body["horizons"]):
        assert_fields(h, "horizon_metrics", f"performance.horizons[{i}]")
        assert 1 <= h["horizon"] <= 30

    # evaluated_at: null BOLEH (belum pernah evaluasi), tapi bila ada harus
    # tz-aware (§1.5). evaluation_range mengikuti keberadaan laporan.
    if body["evaluated_at"] is not None:
        assert_wita(body["evaluated_at"], "performance.evaluated_at")
    if body["evaluation_range"] is not None:
        assert set(body["evaluation_range"]) == {"from", "to"}
        assert_wita(body["evaluation_range"]["from"], "evaluation_range.from")
        assert_wita(body["evaluation_range"]["to"], "evaluation_range.to")


# ===========================================================================
# 7. Envelope error — §3.3 (satu representan: 404 E_NO_FORECAST di latest
#    dengan store kosong, kondisi kontrak yang sah)
# ===========================================================================
def test_error_envelope_contract_fields(client):
    r = client.get("/api/v1/forecast/latest", params={"site_id": SITE},
                   headers=HEADERS)
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"error"}, f"envelope berlebih: {set(body)}"
    assert_fields(body["error"], "error_envelope", "error envelope")
    ErrorEnvelope.model_validate(body)
    assert body["error"]["code"] == "E_NO_FORECAST"
    assert body["error"]["request_id"] == r.headers["X-Request-Id"]


# ===========================================================================
# 8. Cakupan — TABEL DIKUNCI terhadap skema (regresi dua arah)
# ===========================================================================
def test_contract_table_matches_schema_fields():
    """Tabel CONTRACT_FIELDS == field Pydantic — skema berubah tanpa kontrak
    (atau sebaliknya) membuat test ini gagal duluan, bukan integrasi."""
    from app.actual_schemas import ActualPoint
    from app.schemas import (
        ForecastPoint,
        ForecastSummary,
        HorizonMetrics,
        PageInfo,
        ThresholdsResponse,
    )

    pairs = [
        ("health", HealthResponse),
        ("model_info", ModelInfoResponse),
        ("performance", PerformanceResponse),
        ("forecast_post", ForecastResponse),
        ("forecast_latest", ForecastResponse),
        ("history_page", ForecastHistoryResponse),
        ("history_item", ForecastSummary),
        ("history_page_info", PageInfo),
        ("actual", ActualResponse),
        ("forecast_point", ForecastPoint),
        ("actual_point", ActualPoint),
        ("thresholds", ThresholdsResponse),
        ("horizon_metrics", HorizonMetrics),
    ]
    for key, model in pairs:
        # Nama di JSON memakai alias bila ada (mis. from_ → "from").
        schema_fields = {
            (f.alias or name) for name, f in model.model_fields.items()
        }
        optional = OPTIONAL_FIELDS.get(key, set())
        assert CONTRACT_FIELDS[key] | optional == schema_fields, (
            f"{key}: tabel kontrak {sorted(CONTRACT_FIELDS[key])} ≠ "
            f"skema {sorted(schema_fields)} — perbarui kontrak §5 + tabel ini"
        )
        # Aturan §1: field opsional bertanda `| None` (boleh hilang), wajib tidak.
        for name in optional & set(model.model_fields):
            assert model.model_fields[name].is_required() is False, (
                f"{key}.{name} ditandai opsional di tabel tapi wajib di skema"
            )


def test_error_envelope_schema_constants():
    """Envelope error §3.3: tabel selaras ErrorPayload."""
    from app.schemas import ErrorPayload

    assert CONTRACT_FIELDS["error_envelope"] | OPTIONAL_FIELDS["error_envelope"] \
        == set(ErrorPayload.model_fields)


def test_openapi_documents_every_contract_endpoint():
    """§1.2: semua endpoint kontrak tampil di OpenAPI — tak ada yang disembunyikan."""
    with TestClient(create_app()) as c:
        paths = c.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/api/v1/forecast",
        "/api/v1/forecast/actual",
        "/api/v1/forecast/latest",
        "/api/v1/forecast/history",
        "/api/v1/model/info",
        "/api/v1/model/performance",
        "/api/v1/health",
    }, paths


# ---------------------------------------------------------------------------
# Mode script: delegasikan ke pytest (file ini memang suite pytest).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
