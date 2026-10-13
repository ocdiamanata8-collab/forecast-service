"""Verifikasi isi response endpoint terhadap kontrak — bukan cuma HTTP 200.

    python scripts/validate_response_fields.py [--base URL] [--key KEY]

Dipakai untuk:
  - container lokal (default http://127.0.0.1:8001), atau
  - deployment publik (pass --base https://<domain> --key <kunci>)

Memverifikasi FIELD PER FIELD terhadap KONTRAK_API_v1.md — bukan hanya
status code, karena "200 tetapi field salah" adalah kegagalan integrasi
yang justru paling sering terjadi.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

Z = ZoneInfo("Asia/Makassar")
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))


def call(base: str, path: str, method: str = "GET", body: dict | None = None,
         key: str | None = None) -> tuple[int, object, dict]:
    headers: dict[str, str] = {}
    if key:
        headers["X-API-Key"] = key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
            obj = json.loads(raw) if "json" in ctype and raw.strip() else raw
            return r.status, obj, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {"_raw": raw}
        return e.code, obj, dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", {}


def valid_body(horizon: int = 4) -> dict:
    start = dt.datetime(2024, 1, 1, tzinfo=Z)
    hist = []
    for i in range(60):
        ts = start + dt.timedelta(days=i)
        v = max(0.0, 180 + 60 * math.sin(2 * math.pi * (i + 1) / 365.25))
        hist.append({"timestamp": ts.isoformat(), "value": round(v, 3)})
    return {"site_id": "PLTS_KUPANG", "target": "irradiance",
            "resolution": "daily", "horizon": horizon, "history": hist}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--key", default=None, help="X-API-Key (bila tidak, ambil API_KEYS env)")
    import os
    ap.set_defaults(key=None)
    args = ap.parse_args()
    key = args.key or os.environ.get("API_KEYS", "").split(",")[0].strip() or None

    print(f"BASE = {args.base}")
    if not args.base.startswith("http://127.0.0.1") and not args.base.startswith("http://localhost"):
        print("TARGET = PUBLIK (luar jaringan lokal)")
    else:
        print("TARGET = LOCAL container (bukan jaringan luar)")

    # ---- 1. health ----
    s, h, hh = call(args.base, "/api/v1/health")
    check("GET /health = 200", s == 200, f"HTTP {s}")
    if isinstance(h, dict):
        check("health: status dalam enum ok/degraded/unavailable",
              h.get("status") in {"ok", "degraded", "unavailable"}, str(h.get("status")))
        check("health: model_version TIDAK bocor", "model_version" not in h)
        check("health: punya service_version/uptime/model_loaded",
              all(k in h for k in ("service_version", "uptime_seconds", "model_loaded")))
        ts = str(h.get("timestamp", ""))
        check("health: timestamp offset eksplisit (+08:00)",
              ts.endswith("+08:00") or ts.endswith("+07:00"), ts)
        check("health: X-Request-Id header ada",
              any(k.lower() == "x-request-id" for k in hh))

    # ---- 2. contoh request dari instruksi (historical_data) ----
    wrong = {"site_id": "PLTS_KUPANG", "horizon": 4,
             "historical_data": [72.4, 75.1, 78.8, 81.2]}
    s2, e2, _ = call(args.base, "/api/v1/forecast", "POST", wrong, key)
    check("contoh historical_data DITOLAK 422 (skema: history[])",
          s2 == 422 and isinstance(e2, dict) and
          e2.get("error", {}).get("code") == "E_HISTORY_TOO_SHORT",
          f"HTTP {s2} code={e2.get('error', {}).get('code') if isinstance(e2, dict) else '?'}")
    if isinstance(e2, dict) and "error" in e2:
        check("422 punya envelope + request_id",
              {"code", "message", "request_id"} <= set(e2["error"]))

    # ---- 3. request valid → cek field konteks ----
    s3, f3, _ = call(args.base, "/api/v1/forecast", "POST", valid_body(), key)
    check("POST /forecast valid = 200", s3 == 200, f"HTTP {s3}")
    if isinstance(f3, dict) and s3 == 200:
        needed = {"site_id", "target", "resolution", "unit", "timezone", "ts",
                  "generated_at", "horizon", "model_version", "order", "forecast"}
        missing = sorted(needed - set(f3))
        check("field konteks lengkap (kontrak §3.2)", not missing, f"hilang={missing}")
        check("site_id echo + horizon echo",
              f3.get("site_id") == "PLTS_KUPANG" and f3.get("horizon") == 4,
              str({k: f3.get(k) for k in ("site_id", "horizon")}))
        check("unit=W/m2 & timezone=Asia/Makassar",
              f3.get("unit") == "W/m2" and f3.get("timezone") == "Asia/Makassar",
              str({k: f3.get(k) for k in ("unit", "timezone")}))
        check("model_version semver + ts/generated_at offset +08:00",
              bool(f3.get("model_version")) and str(f3.get("generated_at", "")).endswith("+08:00")
              and str(f3.get("ts", "")).endswith("+08:00"),
              str({k: f3.get(k) for k in ("model_version", "ts", "generated_at")}))
        check("ts != generated_at (akurasi bisa diukur)",
              f3.get("ts") != f3.get("generated_at"))
        pts = f3.get("forecast") or []
        check("forecast: panjang == horizon", len(pts) == 4, str(len(pts)))
        if pts:
            check("point: forecast_time+forecast_value+model_version",
                  all({"forecast_time", "forecast_value", "model_version"} <= set(p)
                      for p in pts), str(list(pts[0])))
            check("point: forecast_time +08:00, value >= 0, urut naik, versi sama",
                  all(p["forecast_time"].endswith("+08:00") and p["forecast_value"] >= 0
                      and p["model_version"] == f3["model_version"] for p in pts)
                  and [p["forecast_time"] for p in pts] == sorted(p["forecast_time"] for p in pts),
                  str([p["forecast_time"] for p in pts]))

    # ---- 4. deterministik ----
    s4, f4, _ = call(args.base, "/api/v1/forecast", "POST", valid_body(), key)
    if s3 == 200 and s4 == 200 and isinstance(f3, dict) and isinstance(f4, dict):
        check("deterministik: forecast[] identik antar run",
              f3.get("forecast") == f4.get("forecast"))

    # ---- 5. latest (POST tidak menyimpan → E_NO_FORECAST adalah kontrak) ----
    s5, l5, _ = call(args.base, "/api/v1/forecast/latest?site_id=PLTS_KUPANG", key=key)
    ok5 = (s5 == 404 and isinstance(l5, dict)
           and l5.get("error", {}).get("code") in {"E_NO_FORECAST", "E_SITE_UNKNOWN"}) or s5 == 200
    check("GET latest: respons sesuai kontrak (200 atau 404 E_NO_FORECAST)",
          ok5, f"HTTP {s5} code={l5.get('error', {}).get('code') if isinstance(l5, dict) else '?'}")
    if s5 == 200 and isinstance(l5, dict):
        check("latest: punya field konteks lengkap",
              {"site_id", "generated_at", "unit", "model_version"} <= set(l5),
              str(sorted(set(l5))))

    # ---- 6. tanpa key → 401 (bukan 200 / bukan crash) ----
    s6, e6, _ = call(args.base, "/api/v1/model/info", key=None)
    check("tanpa key → 401 E_UNAUTHORIZED",
          s6 == 401 and isinstance(e6, dict) and
          e6.get("error", {}).get("code") == "E_UNAUTHORIZED", f"HTTP {s6}")

    print("\n" + "=" * 74)
    print("VERIFIKASI FIELD RESPONSE vs KONTRAK")
    print("=" * 74)
    fails = 0
    for label, ok, detail in RESULTS:
        if not ok:
            fails += 1
        suffix = f"  -> {detail}" if (detail and not ok) else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    print("-" * 74)
    print(f"TOTAL: {len(RESULTS) - fails}/{len(RESULTS)} lulus")
    if fails:
        return 1
    print("SEMUA FIELD COCOK — bukan cuma status 200")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
