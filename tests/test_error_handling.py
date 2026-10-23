"""Uji error eksplisit: pesan + kode yang layak ditampilkan monitoring.

    pytest tests/test_error_handling.py -q

Empat kasus yang diminta (kontrak §6.1), plus dua jaminan pendukung:

    HTTP 422  data kosong / horizon di luar batas  -> pesan spesifik, bukan generik
    HTTP 404  site_id tidak dikenal                -> E_SITE_UNKNOWN, bukan E_NO_FORECAST
    HTTP 503  model belum siap                     -> E_SERVICE_UNAVAILABLE + Retry-After
    HTTP 429  terlalu sering                       -> Retry-After + X-RateLimit-Remaining

Dan yang paling penting untuk integrasi:

    setiap response error berbentuk ErrorEnvelope §3.3, TANPA stack trace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DEMO_KEY = "dev-key-change-me-0123456789"
# Set API key di environment SEBELUM lifespan berjalan (tidak ada fallback
# di app/main.py). conftest.py hanya menangani mode pytest; script mode
# perlu mengaturnya sendiri.
os.environ.setdefault("API_KEYS", DEMO_KEY)
# Mode script tidak memuat conftest — matikan scheduler agar store tetap
# kosong (default test: latest = 404 E_NO_FORECAST). Lihat tests/test_scheduler.py
# untuk versi scheduler yang sengaja diaktifkan.
os.environ.setdefault("SCHEDULER_ENABLED", "false")
K = {"X-API-Key": DEMO_KEY}
SITE = "PLTS_KUPANG"

#: Tanda stack trace / exception Python yang tidak boleh bocor ke client.
LEAK_MARKERS = (
    "Traceback", 'File "', ".py\"", "Exception", "pydantic",
    "raise ", "line ", "KeyError", "ValueError", "TypeError",
)


def history_payload(n: int = 60) -> list[dict[str, object]]:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    v = np.clip(180 + 60 * np.sin(2 * np.pi * idx.dayofyear.to_numpy(float) / 365.25), 0, None)
    return [{"timestamp": t.isoformat(), "value": float(x)} for t, x in zip(idx, v)]


def body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "site_id": SITE, "target": "irradiance",
        "resolution": "daily", "horizon": 7, "history": history_payload(),
    }
    base.update(over)
    return base


def assert_envelope(r, status: int, code: str) -> dict[str, object]:
    """Respons wajib: status benar, envelope §3.3 utuh, tanpa stack trace."""
    assert r.status_code == status, f"status {r.status_code}, diharapkan {status}: {r.text[:300]}"

    # Tidak boleh berupa HTML error page / traceback.
    for marker in LEAK_MARKERS:
        assert marker not in r.text, f"exception mentah bocor ({marker!r}): {r.text[:400]}"

    payload = r.json()
    assert "error" in payload, f"bukan ErrorEnvelope: {payload}"
    err = payload["error"]
    assert err["code"] == code, f"kode {err['code']!r}, diharapkan {code!r}"
    assert err["message"], "message kosong — monitoring tidak bisa menampilkan apa-apa"
    assert err["request_id"] == r.headers["X-Request-Id"], "request_id tidak sinkron"
    assert isinstance(err["message"], str) and err["message"].strip() != ""
    return err


# ===========================================================================
# 422 — data kosong / horizon di luar batas
# ===========================================================================
def test_empty_history_422_with_specific_message(client):
    err = assert_envelope(
        client.post("/api/v1/forecast", json=body(history=[]), headers=K),
        422, "E_HISTORY_TOO_SHORT",
    )
    assert "validation failed" not in err["message"].lower(), \
        "pesan generik — monitoring hanya mendapat 'request validation failed'"
    assert "30" in err["message"], f"pesan tidak menjelaskan syarat: {err['message']}"
    # details[] wajib untuk 422 (kontrak §3.3)
    assert err.get("details"), "422 tanpa details[]"


def test_near_empty_history_422(client):
    assert_envelope(
        client.post("/api/v1/forecast", json=body(history=history_payload(5)), headers=K),
        422, "E_HISTORY_TOO_SHORT",
    )


def test_horizon_above_bound_422(client):
    err = assert_envelope(
        client.post("/api/v1/forecast", json=body(horizon=31), headers=K),
        422, "E_HORIZON_RANGE",
    )
    assert "1-30" in err["message"], f"pesan tidak menyebut batas: {err['message']}"
    detail_fields = {d["field"] for d in err["details"]}
    assert "horizon" in detail_fields, detail_fields


def test_horizon_below_bound_422(client):
    assert_envelope(
        client.post("/api/v1/forecast", json=body(horizon=0), headers=K),
        422, "E_HORIZON_RANGE",
    )


def test_naive_timestamp_422_mentions_timezone(client):
    bad = history_payload()
    bad[0]["timestamp"] = "2024-01-01T00:00:00"
    err = assert_envelope(
        client.post("/api/v1/forecast", json=body(history=bad), headers=K),
        422, "E_TZ_REQUIRED",
    )
    assert "offset" in err["message"].lower() or "timezone" in err["message"].lower()


def test_validation_messages_are_in_english(client):
    """Kontrak §3.3: pesan Inggris — platform monitoring menampilkannya apa adanya."""
    cases = [
        (body(history=[]), "E_HISTORY_TOO_SHORT"),
        (body(horizon=99), "E_HORIZON_RANGE"),
        (body(site_id="plts-kupang"), "E_VALIDATION_FAILED"),
    ]
    for payload, _ in cases:
        r = client.post("/api/v1/forecast", json=payload, headers=K)
        assert r.status_code == 422
        msg = r.json()["error"]["message"]
        ascii_only = msg.isascii()
        assert ascii_only, f"pesan non-ASCII (kemungkinan bahasa lokal): {msg!r}"


# ===========================================================================
# 404 — site_id tidak dikenal (dibedakan dari belum berforecast)
# ===========================================================================
def test_unknown_site_404_site_unknown(client):
    err = assert_envelope(
        client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_TIDAK_ADA"},
                   headers=K),
        404, "E_SITE_UNKNOWN",
    )
    assert "PLTS_TIDAK_ADA" in err["message"], "pesan tidak menyebut site bermasalah"
    assert "SITE_IDS" in err["message"] or "registered" in err["message"], \
        f"pesan tidak menunjuk perbaikan: {err['message']}"


def test_known_site_without_forecast_404_no_forecast(client):
    """Beda kode dari kasus di atas — dua kondisi berbeda, dua kode berbeda."""
    err = assert_envelope(
        client.get("/api/v1/forecast/latest", params={"site_id": SITE}, headers=K),
        404, "E_NO_FORECAST",
    )
    assert "scheduler" in err["message"] or "generated yet" in err["message"]


def test_unknown_site_also_rejected_on_history_and_performance(client):
    for path, params in (
        ("/api/v1/forecast/history", {"site_id": "PLTS_NYASAR"}),
        ("/api/v1/model/performance", {"site_id": "PLTS_NYASAR"}),
    ):
        assert_envelope(client.get(path, params=params, headers=K), 404, "E_SITE_UNKNOWN")


def test_unknown_site_404_on_forecast_post(client):
    """POST /forecast punya site_id di body — situs tak dikenal harus ketahuan."""
    r = client.post("/api/v1/forecast", json=body(site_id="PLTS_NYASAR"), headers=K)
    # Kode apa pun yang terpicu, ia harus envelope 4xx, bukan 200.
    assert r.status_code in (404, 422), f"{r.status_code}: {r.text[:200]}"
    assert "error" in r.json()


# ===========================================================================
# 503 — model belum siap
# ===========================================================================
def test_model_not_ready_503_on_forecast(client):
    client.app.state.model_loaded = False
    r = client.post("/api/v1/forecast", json=body(), headers=K)
    err = assert_envelope(r, 503, "E_SERVICE_UNAVAILABLE")
    assert r.headers.get("Retry-After"), "503 wajib membawa Retry-After agar client tau kapan ulang"
    assert "health" in err["message"], \
        f"pesan harus mengarahkan ke /health: {err['message']}"


def test_model_not_ready_503_on_model_info(client):
    client.app.state.model_loaded = False
    assert_envelope(client.get("/api/v1/model/info", headers=K), 503, "E_SERVICE_UNAVAILABLE")


def test_health_still_answers_when_model_not_ready(client):
    """/health harus tetap 200 — itu yang dipakai probe deploy untuk mendeteksi."""
    client.app.state.model_loaded = False
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"


def test_latest_still_readable_when_model_not_ready(client):
    """latest/history membaca penyimpanan — tetap sah walau model sedang reload,
    sehingga monitoring lama tidak ikut mati saat deploy."""
    from app.schemas import ForecastPoint
    from app.storage import items_from_response
    from app.version import model_version

    base = pd.Timestamp("2026-10-01T06:00:00+08:00")
    pt = ForecastPoint(forecast_time=base + pd.Timedelta(days=1),
                       forecast_value=212.5, model_version=model_version())
    client.app.state.store.save(items_from_response(
        # §3.2: forecast_id = UUID (string sembarangan ditolak penyedia DB)
        forecast_id="00000000-0000-4000-8000-000000000001",
        site_id=SITE, target="irradiance",
        resolution="daily", unit="W/m2", ts=base, generated_at=base,
        model_version=model_version(), points=[pt]))

    client.app.state.model_loaded = False
    r = client.get("/api/v1/forecast/latest", params={"site_id": SITE}, headers=K)
    assert r.status_code == 200, \
        "latest ikut terkunci saat model reload — monitoring kehilangan data lama"


# ===========================================================================
# 429 — terlalu sering
# ===========================================================================
def test_rate_limit_429_with_retry_headers(client):
    last = None
    for _ in range(65):
        last = client.get("/api/v1/model/performance",
                          params={"site_id": SITE}, headers=K)
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429, \
        f"rate limit tidak terpicu dalam 65 request (status {last.status_code})"

    err = assert_envelope(last, 429, "E_RATE_LIMITED")
    assert last.headers.get("Retry-After"), "429 tanpa Retry-After"
    assert last.headers.get("X-RateLimit-Remaining") == "0"
    assert last.headers.get("X-RateLimit-Limit") == "60"
    # §1.3: X-RateLimit-Reset wajib di 429 (detik sampai kuota kembali).
    reset = last.headers.get("X-RateLimit-Reset")
    assert reset is not None and reset.isdigit() and int(reset) >= 1, \
        f"429 tanpa X-RateLimit-Reset yang sah (dapat {reset!r})"
    # Pesan mengarahkan ke fallback sesuai instruksi: pakai hasil latest.
    assert "latest" in err["message"].lower() or "limit" in err["message"].lower()


# ===========================================================================
# Jaminan umum — envelope, tanpa traceback, status konsisten
# ===========================================================================
def test_all_error_responses_are_envelopes_without_traceback(client):
    """Sweep: setiap jalur error yang dipicu harus berbentuk envelope §3.3."""
    failures: list[str] = []

    cases = [
        ("POST", "/api/v1/forecast", body(history=[]), K, 422, "E_HISTORY_TOO_SHORT"),
        ("POST", "/api/v1/forecast", body(horizon=31), K, 422, "E_HORIZON_RANGE"),
        ("GET", "/api/v1/forecast/latest?site_id=PLTS_NYASAR", None, K, 404, "E_SITE_UNKNOWN"),
        ("GET", "/api/v1/model/info", None, {}, 401, "E_UNAUTHORIZED"),
        ("GET", "/api/v1/model/info", None, {"X-API-Key": "x" * 20}, 403, "E_FORBIDDEN"),
        ("GET", "/api/v1/tidak-ada", None, K, 404, "E_NOT_FOUND"),
        ("GET", "/api/v1/forecast/history?site_id=PLTS_KUPANG&cursor=bodoh",
         None, K, 400, "E_CURSOR_INVALID"),
        ("GET", "/api/v1/forecast/history?site_id=PLTS_KUPANG&limit=999",
         None, K, 422, "E_LIMIT_RANGE"),
    ]
    for method, path, payload, headers, status, code in cases:
        if method == "POST":
            r = client.post(path, json=payload, headers=headers)
        else:
            r = client.get(path, headers=headers)
        try:
            assert_envelope(r, status, code)
        except AssertionError as exc:
            failures.append(f"{method} {path}: {exc}")

    assert not failures, "\n".join(failures)


def test_no_traceback_in_any_non_2xx(client):
    """Client monitoring tidak boleh pernah menerima stack trace Python."""
    probes = [
        ("POST", "/api/v1/forecast", body(history=[])),
        ("POST", "/api/v1/forecast", body(horizon="bukan-angka")),
        ("GET", "/api/v1/forecast/latest?site_id=PLTS_NYASAR", None),
        ("GET", "/api/v1/tidak-ada", None),
    ]
    for method, path, payload in probes:
        r = client.post(path, json=payload, headers=K) if method == "POST" \
            else client.get(path, headers=K)
        assert r.status_code >= 400
        for marker in LEAK_MARKERS:
            assert marker not in r.text, \
                f"{method} {path}: marker {marker!r} bocor di {r.status_code}"


def test_error_payload_never_contains_model_details(client):
    """4xx tidak boleh membocorkan detail internal model (order, artefak, dsb)."""
    r = client.post("/api/v1/forecast", json=body(history=[]), headers=K)
    text = r.text.lower()
    for internal in ("artifacts.json", "statsmodels", "traceback", ".py"):
        assert internal not in text, f"detail internal bocor: {internal}"


# ===========================================================================
# Mode script
# ===========================================================================
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    print("\n[1] 422 — data kosong / horizon di luar batas")
    with TestClient(create_app()) as c:
        r = c.post("/api/v1/forecast", json=body(history=[]), headers=K)
        e = r.json().get("error", {})
        check(r.status_code == 422 and e.get("code") == "E_HISTORY_TOO_SHORT",
              f"history kosong -> 422 {e.get('code')}")
        check("30" in e.get("message", ""), f"pesan menjelaskan syarat: {e.get('message', '')[:60]}")

        r = c.post("/api/v1/forecast", json=body(horizon=31), headers=K)
        e = r.json().get("error", {})
        check(r.status_code == 422 and e.get("code") == "E_HORIZON_RANGE",
              f"horizon 31 -> 422 {e.get('code')}")
        check("1-30" in e.get("message", ""), f"pesan menyebut batas: {e.get('message', '')[:60]}")

        check("validation failed" not in r.json()["error"]["message"].lower(),
              "tidak ada pesan generik 'request validation failed'")

    print("\n[2] 404 — site_id tidak dikenal")
    with TestClient(create_app()) as c:
        r = c.get("/api/v1/forecast/latest", params={"site_id": "PLTS_TIDAK_ADA"}, headers=K)
        e = r.json().get("error", {})
        check(r.status_code == 404 and e.get("code") == "E_SITE_UNKNOWN",
              f"site tak dikenal -> 404 {e.get('code')}")

        r = c.get("/api/v1/forecast/latest", params={"site_id": SITE}, headers=K)
        e = r.json().get("error", {})
        check(e.get("code") == "E_NO_FORECAST",
              f"site valid, belum berforecast -> {e.get('code')} (berbeda kode)")

        r = c.get("/api/v1/model/performance", params={"site_id": "PLTS_NYASAR"}, headers=K)
        check(r.json().get("error", {}).get("code") == "E_SITE_UNKNOWN",
              "performance juga memvalidasi site")

    print("\n[3] 503 — model belum siap")
    with TestClient(create_app()) as c:
        c.app.state.model_loaded = False
        r = c.post("/api/v1/forecast", json=body(), headers=K)
        e = r.json().get("error", {})
        check(r.status_code == 503 and e.get("code") == "E_SERVICE_UNAVAILABLE",
              f"forecast saat model tak siap -> 503 {e.get('code')}")
        check(bool(r.headers.get("Retry-After")), f"Retry-After={r.headers.get('Retry-After')}")
        check("health" in e.get("message", ""), "pesan mengarahkan ke /health")

        rh = c.get("/api/v1/health")
        check(rh.status_code == 200 and rh.json()["status"] == "unavailable",
              f"/health tetap 200, status={rh.json()['status']}")

    print("\n[4] 429 — terlalu sering")
    with TestClient(create_app()) as c:
        last = None
        for _ in range(65):
            last = c.get("/api/v1/model/performance", params={"site_id": SITE}, headers=K)
            if last.status_code == 429:
                break
        ok = last is not None and last.status_code == 429
        check(ok, f"rate limit terpicu (status {last.status_code if last else '-'})")
        if ok:
            check(bool(last.headers.get("Retry-After")),
                  f"Retry-After={last.headers.get('Retry-After')}")
            check(last.headers.get("X-RateLimit-Remaining") == "0",
                  "X-RateLimit-Remaining=0")

    print("\n[5] TANPA STACK TRACE di jalur mana pun")
    with TestClient(create_app()) as c:
        probes = [
            ("POST", "/api/v1/forecast", body(history=[])),
            ("POST", "/api/v1/forecast", body(horizon="abc")),
            ("GET", "/api/v1/forecast/latest?site_id=PLTS_NYASAR", None),
            ("GET", "/api/v1/tidak-ada", None),
            ("GET", "/api/v1/model/info", None),
        ]
        leaks = []
        for method, path, payload in probes:
            r = c.post(path, json=payload, headers=K) if method == "POST" \
                else c.get(path, headers=K)
            for m in LEAK_MARKERS:
                if m in r.text:
                    leaks.append(f"{path} -> {m}")
        check(not leaks, f"tanpa marker traceback (bocor: {leaks})")

        # Setiap 4xx harus envelope §3.3
        bad = []
        for method, path, payload in probes:
            r = c.post(path, json=payload, headers=K) if method == "POST" \
                else c.get(path, headers=K)
            if r.status_code >= 400 and "error" not in r.json():
                bad.append(f"{path} ({r.status_code})")
        check(not bad, f"semua 4xx/5xx berbentuk envelope (tanpa: {bad})")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ERROR EKSPLISIT — kode & pesan layak ditampilkan monitoring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
