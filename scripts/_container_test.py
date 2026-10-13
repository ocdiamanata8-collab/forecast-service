"""Uji container forecast-svc di port 8001 — request nyata via host.

    python scripts/_container_test.py
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8001"
KEY = "test-key-01234567890123456789"
H = {"X-API-Key": KEY}
WIB8 = timezone(timedelta(hours=8))
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))


def call(path: str, headers: dict | None = None, method: str = "GET",
         body: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(BASE + path, method=method,
                                 headers=dict(headers or {}))
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
            # /docs, /, /redoc membalas HTML — hanya parse JSON bila memang
            # JSON. Tanpa ini, json.loads melempar dan disalahartikan sebagai
            # status 0 (koneksi gagal), padahal container membalas 200.
            if "json" in ctype:
                try:
                    return r.status, (json.loads(raw) if raw else "")
                except json.JSONDecodeError:
                    return r.status, raw
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.strip().startswith("{") else raw)
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def sample_body() -> dict:
    pts = []
    for i in range(60):
        ts = datetime(2024, 1, 1, tzinfo=WIB8) + timedelta(days=i)
        v = max(0.0, 180 + 60 * math.sin(2 * math.pi * (i + 1) / 365.25))
        pts.append({"timestamp": ts.isoformat(), "value": round(v, 3)})
    return {"site_id": "PLTS_KUPANG", "target": "irradiance",
            "resolution": "daily", "horizon": 7, "history": pts}


def main() -> int:
    # 1. health tanpa key
    s, b = call("/api/v1/health")
    check("GET /health (tanpa key) = 200", s == 200, str(s))
    if isinstance(b, dict):
        check("health: status ok", b.get("status") == "ok", str(b.get("status")))
        check("health: model_loaded true", b.get("model_loaded") is True)
        check("health: service_version terbawa", bool(b.get("service_version")))

    # 2. docs
    s, b = call("/docs")
    check("GET /docs = 200 + Swagger", s == 200 and "swagger" in str(b).lower(), str(s))

    # 3. dashboard (membuktikan templates/ ikut image)
    s, b = call("/")
    check("GET / = 200 + dashboard", s == 200 and "Engineer Console" in str(b), str(s))

    # 4. POST /forecast — request contoh sesuai kontrak
    s, b = call("/api/v1/forecast", headers=H, method="POST", body=sample_body())
    check("POST /forecast = 200", s == 200, f"{s} {str(b)[:200]}")
    if isinstance(b, dict) and s == 200:
        check("POST /forecast: 7 titik", len(b.get("forecast", [])) == 7,
              str(len(b.get("forecast", []))))
        check("POST /forecast: konteks lengkap",
              all(k in b for k in ("site_id", "generated_at", "unit", "model_version")),
              str([k for k in ("site_id", "generated_at", "unit", "model_version") if k not in b]))
        check("POST /forecast: forecast_time +08:00",
              all(p["forecast_time"].endswith("+08:00") for p in b.get("forecast", [])))
        check("POST /forecast: unit W/m2", b.get("unit") == "W/m2", str(b.get("unit")))
        check("POST /forecast: deterministik (forecast[] identik)",
              call("/api/v1/forecast", headers=H, method="POST", body=sample_body())[1]
              .get("forecast") == b.get("forecast"))

    # 5. actual (membuktikan data/actuals.csv ikut image)
    s, b = call("/api/v1/forecast/actual?site_id=PLTS_KUPANG"
                "&from=2024-03-01T00:00:00%2B08:00&to=2024-03-07T00:00:00%2B08:00",
                headers=H)
    check("GET /forecast/actual = 200", s == 200, f"{s} {str(b)[:200]}")
    if isinstance(b, dict) and s == 200:
        check("/forecast/actual: 7 titik aktual", b.get("n") == 7, str(b.get("n")))

    # 6. performance (membuktikan data/evaluation/ ikut image)
    s, b = call("/api/v1/model/performance?site_id=PLTS_KUPANG", headers=H)
    check("GET /model/performance = 200", s == 200, str(s))
    if isinstance(b, dict) and s == 200:
        check("performance: status enum + data_rows",
              b.get("status") in {"GO", "NO_GO", "UNKNOWN"} and "data_rows" in b,
              f"{b.get('status')} data_rows={b.get('data_rows')}")

    # 7. tanpa key -> 401
    s, b = call("/api/v1/model/info")
    check("tanpa key -> 401 E_UNAUTHORIZED",
          s == 401 and isinstance(b, dict)
          and b.get("error", {}).get("code") == "E_UNAUTHORIZED", str(s))

    # 8. openapi 7 endpoint
    s, b = call("/openapi.json")
    check("OpenAPI = 7 endpoint",
          s == 200 and len(b.get("paths", {})) == 7,
          str(len(b.get("paths", {})) if isinstance(b, dict) else s))

    print("\n" + "=" * 70)
    print(f"UJI CONTAINER FORECAST-SVC DI HOST {BASE}")
    print("=" * 70)
    fails = 0
    for label, cond, detail in RESULTS:
        if not cond:
            fails += 1
        suffix = f"  -> {detail}" if (detail and not cond) else ""
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}{suffix}")
    print("-" * 70)
    print(f"TOTAL: {len(RESULTS) - fails}/{len(RESULTS)} lulus")
    if fails:
        return 1
    print("CONTAINER LAYAN PENUH DARI HOST — TERMASUK POST /forecast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
