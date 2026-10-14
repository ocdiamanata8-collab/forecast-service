"""Validasi field response publik terhadap KONTRAK_API_v1.md.

Contoh setelah deploy (PowerShell):
    $env:API_KEYS='isi-kunci-yang-diset-di-host'
    python scripts/validate_response_fields.py --base https://domain-anda.com

Atau langsung:
    python scripts/validate_response_fields.py --base https://domain-anda.com --key KUNCI

Tanpa key, script hanya memeriksa health publik lalu keluar dengan kode 2
(validasi parsial, bukan kegagalan service). Tidak pernah mencetak key ke log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

SITE_TZ = ZoneInfo("Asia/Makassar")
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))


def call(base: str, path: str, method: str = "GET", body: dict | None = None,
         key: str | None = None) -> tuple[int, object, dict[str, str]]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            ctype = response.headers.get("Content-Type", "")
            parsed = json.loads(raw) if "json" in ctype and raw.strip() else raw
            return response.status, parsed, dict(response.headers.items())
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        return error.code, parsed, dict(error.headers.items())
    except Exception as error:  # noqa: BLE001 - tampilkan ringkas, tanpa traceback.
        return 0, f"{type(error).__name__}: {error}", {}


def history_example(horizon: int = 4) -> dict[str, object]:
    start = dt.datetime(2024, 1, 1, tzinfo=SITE_TZ)
    history = []
    for i in range(60):
        timestamp = start + dt.timedelta(days=i)
        value = max(0.0, 180.0 + 60.0 * math.sin(2.0 * math.pi * (i + 1) / 365.25))
        history.append({"timestamp": timestamp.isoformat(), "value": round(value, 3)})
    return {
        "site_id": "PLTS_KUPANG",
        "target": "irradiance",
        "resolution": "daily",
        "horizon": horizon,
        "history": history,
    }


def has_offset(value: object, offset: str = "+08:00") -> bool:
    return isinstance(value, str) and value.endswith(offset)


def print_results(base: str, partial: bool = False) -> int:
    print("\n" + "=" * 76)
    print("PUBLIC RESPONSE FIELD VALIDATION")
    print(f"Base URL: {base}")
    if partial:
        print("Scope: health only (no API key supplied)")
    print("=" * 76)
    failures = 0
    for label, passed, detail in RESULTS:
        failures += not passed
        suffix = f" -- {detail}" if detail and not passed else ""
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}{suffix}")
    print("-" * 76)
    passed_count = len(RESULTS) - failures
    print(f"TOTAL: {passed_count}/{len(RESULTS)} passed")
    if failures:
        print("Validation failed: public response does not match the contract.")
        return 1
    if partial:
        print("PARTIAL: health passed; rerun with --key to validate protected endpoints.")
        return 2
    print("ALL CHECKS PASSED: response fields match the API contract.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8001",
                        help="base URL, e.g. https://domain-anda.com")
    parser.add_argument("--key", default=None,
                        help="X-API-Key; otherwise reads first key from API_KEYS")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    key = args.key or os.environ.get("API_KEYS", "").split(",", 1)[0].strip() or None

    is_local = base.startswith(("http://127.0.0.1", "http://localhost"))
    print(f"Target: {'local' if is_local else 'public'} ({base})")

    # Public health probe, no credentials.
    status, health, headers = call(base, "/api/v1/health")
    check("GET /health returns 200", status == 200, f"HTTP {status}")
    if status == 200 and isinstance(health, dict):
        check("health status is ok/degraded/unavailable",
              health.get("status") in {"ok", "degraded", "unavailable"},
              str(health.get("status")))
        check("health exposes service_version, uptime_seconds, model_loaded",
              all(field in health for field in
                  ("service_version", "uptime_seconds", "model_loaded")))
        check("health does not expose model_version", "model_version" not in health)
        check("health timestamp is timezone-aware +08:00",
              has_offset(health.get("timestamp")))
        check("health includes X-Request-Id",
              any(name.lower() == "x-request-id" for name in headers))
    elif isinstance(health, dict):
        check("health response uses error envelope", "error" in health, str(health)[:160])

    if not key:
        print_results(base, partial=True)
        return 2

    # Wrong example from the initial integration proposal: must not be mistaken
    # for the actual v1 contract (which requires timestamped history points).
    wrong_example = {
        "site_id": "PLTS_KUPANG",
        "horizon": 4,
        "historical_data": [72.4, 75.1, 78.8, 81.2],
    }
    status, error_body, _ = call(base, "/api/v1/forecast", "POST", wrong_example, key)
    error_code = error_body.get("error", {}).get("code") if isinstance(error_body, dict) else None
    check("legacy historical_data example is rejected with 422",
          status == 422 and error_code in {"E_HISTORY_TOO_SHORT", "E_VALIDATION_FAILED"},
          f"HTTP {status}; code={error_code}")
    if isinstance(error_body, dict) and isinstance(error_body.get("error"), dict):
        check("422 envelope has code, message, request_id",
              {"code", "message", "request_id"} <= set(error_body["error"]))

    # Valid v1 forecast request.
    request = history_example(horizon=4)
    status, forecast, _ = call(base, "/api/v1/forecast", "POST", request, key)
    check("POST /forecast returns 200", status == 200, f"HTTP {status}: {str(forecast)[:160]}")
    if status == 200 and isinstance(forecast, dict):
        top_fields = {
            "site_id", "target", "resolution", "unit", "timezone", "ts",
            "generated_at", "horizon", "model_version", "order", "forecast",
        }
        missing = sorted(top_fields - set(forecast))
        check("forecast response has all contract fields", not missing,
              f"missing: {missing}")
        check("site_id and horizon echo the request",
              forecast.get("site_id") == request["site_id"]
              and forecast.get("horizon") == request["horizon"])
        check("target irradiance uses unit W/m2 and Asia/Makassar",
              forecast.get("target") == "irradiance"
              and forecast.get("unit") == "W/m2"
              and forecast.get("timezone") == "Asia/Makassar")
        check("ts and generated_at have explicit +08:00 and differ",
              has_offset(forecast.get("ts"))
              and has_offset(forecast.get("generated_at"))
              and forecast.get("ts") != forecast.get("generated_at"))
        check("model_version is present",
              bool(re.match(r"^v\d+\.\d+(\.\d+)?$",
                            str(forecast.get("model_version", "")))))
        points = forecast.get("forecast") or []
        check("forecast point count equals horizon",
              len(points) == forecast.get("horizon"))
        point_fields_ok = all(
            {"forecast_time", "forecast_value", "model_version"} <= set(point)
            and has_offset(point.get("forecast_time"))
            and isinstance(point.get("forecast_value"), (int, float))
            and point["forecast_value"] >= 0
            and point.get("model_version") == forecast.get("model_version")
            for point in points
        )
        ordered = [point.get("forecast_time") for point in points]
        check("every forecast point has typed fields, +08:00, nonnegative value",
              point_fields_ok)
        check("forecast timestamps are sorted ascending and unique",
              ordered == sorted(ordered) and len(ordered) == len(set(ordered)))

        # Determinism compares forecast[] only. generated_at is intentionally
        # different for separate requests (the request-time context field).
        status2, second, _ = call(base, "/api/v1/forecast", "POST", request, key)
        check("identical requests produce identical forecast[]",
              status2 == 200 and isinstance(second, dict)
              and second.get("forecast") == forecast.get("forecast"))

    # Latest may legitimately be 404 because POST is on-demand and does not
    # store the result; it must distinguish unknown site vs no saved forecast.
    status, latest, _ = call(base, "/api/v1/forecast/latest?site_id=PLTS_KUPANG", key=key)
    latest_code = latest.get("error", {}).get("code") if isinstance(latest, dict) else None
    latest_ok = (status == 200 or
                 (status == 404 and latest_code in {"E_NO_FORECAST", "E_SITE_UNKNOWN"}))
    check("GET latest returns contract response or explicit no-forecast 404",
          latest_ok, f"HTTP {status}; code={latest_code}")
    if status == 200 and isinstance(latest, dict):
        check("latest response carries site_id, generated_at, unit, model_version",
              {"site_id", "generated_at", "unit", "model_version"} <= set(latest))

    # Missing credentials are an explicit 401, not a raw exception.
    status, unauthorized, _ = call(base, "/api/v1/model/info", key=None)
    check("request without key returns 401 E_UNAUTHORIZED",
          status == 401 and isinstance(unauthorized, dict)
          and unauthorized.get("error", {}).get("code") == "E_UNAUTHORIZED",
          f"HTTP {status}")

    return print_results(base)


if __name__ == "__main__":
    raise SystemExit(main())
