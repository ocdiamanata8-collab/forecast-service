"""Validasi end-to-end terhadap server LIVE (port 8000) via httpx.

Memvalidasi setiap response field-per-field terhadap lembar spesifikasi:
  - SPESIFIKASI_KONTRAK_MODEL.md §2 (output model)
  - KONTRAK_API_v1.md §3.1/§3.2 (field konteks), §5.x (per endpoint)
  - KONTRAK_API_v1.md §6.1 (kode error)

Bukan test unit: semua request mengenai uvicorn yang sedang berjalan,
dengan urutan seperti yang dilakukan engineer di /docs.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import os

import httpx
import pandas as pd

BASE = "http://127.0.0.1:8000"
# Ambil dari environment — TIDAK ada fallback key di kode (kebijakan repo
# hygiene: kebocoran API key adalah masalah termahal di tahap ini). Tanpa
# API_KEYS, script ini berhenti dengan pesan jelas, bukan diam-diam
# memakai kunci yang bisa saja sudah ter-commit di commit sebelumnya.
_env_key = os.environ.get("API_KEYS", "").split(",")[0].strip()
if not _env_key:
    print("ERROR: environment variable API_KEYS kosong.")
    print("Set dulu, mis.:  $env:API_KEYS='kunci-lokal-12345678901234567890'")
    print("Lihat .env.example — tanpa itu endpoint ber-key membalas 401.")
    raise SystemExit(2)
KEY = _env_key
H = {"X-API-Key": KEY}

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))


def history_payload(n: int = 60) -> list[dict]:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    v = (180 + 60 * np_sin(idx)).clip(0, None)
    return [{"timestamp": t.isoformat(), "value": float(x)} for t, x in zip(idx, v)]


def np_sin(idx):
    import numpy as np

    return 60 * np.sin(2 * np.pi * idx.dayofyear.to_numpy(float) / 365.25) + 60


def tz_aware(s: str) -> bool:
    """RFC3339 dengan offset eksplisit (bukan Z tunggal)."""
    return bool(re.search(r"[+-]\d{2}:\d{2}$", s))


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0)

    # ------------------------------------------------------------------
    # 1. Swagger terbuka
    # ------------------------------------------------------------------
    r = client.get("/docs")
    check("GET /docs = 200 dan Swagger UI", r.status_code == 200 and "swagger" in r.text.lower(),
          f"{r.status_code}")

    r = client.get("/openapi.json")
    paths = set(r.json()["paths"])
    check("OpenAPI = 7 endpoint kontrak (6 lama + actual non-breaking)",
          len(paths) == 7, f"{len(paths)}: {sorted(paths)}")
    schemes = r.json().get("components", {}).get("securitySchemes", {})
    # Skema bisa bernama internal (mis. ApiKeyAuth); yang penting nama
    # header yang dikirimnya X-API-Key.
    check("securitySchemes mendefinisikan header X-API-Key",
          any(s.get("name") == "X-API-Key" and s.get("in") == "header"
              for s in schemes.values()),
          str(schemes))
    health_sec = r.json()["paths"]["/api/v1/health"]["get"].get("security")
    check("/health TIDAK butuh key (security=None)", health_sec in (None, []),
          f"security={health_sec}")
    post_sec = r.json()["paths"]["/api/v1/forecast"]["post"].get("security")
    check("/forecast butuh key (security=[{ApiKeyAuth}])",
          post_sec == [{"ApiKeyAuth": []}], str(post_sec))

    # ------------------------------------------------------------------
    # 2. GET / (dashboard)
    # ------------------------------------------------------------------
    r = client.get("/")
    check("GET / = 200 HTML dashboard", r.status_code == 200 and "text/html" in r.headers["content-type"],
          f"{r.status_code}")
    check("dashboard memanggil /api/v1/forecast/latest",
          "/api/v1/forecast/latest" in r.text)

    # ------------------------------------------------------------------
    # 3. GET /api/v1/health — kontrak §5.6
    # ------------------------------------------------------------------
    r = client.get("/api/v1/health")
    b = r.json()
    check("health = 200", r.status_code == 200, str(r.status_code))
    allowed = {"status", "service_version", "timestamp", "uptime_seconds", "model_loaded"}
    check("health: field persis daftar §5.6", set(b) == allowed, str(set(b)))
    check("health: status dalam enum ok/degraded/unavailable",
          b["status"] in {"ok", "degraded", "unavailable"}, b["status"])
    check("health: model_version TIDAK bocor (§1.1)", "model_version" not in b)
    check("health: timestamp tz-aware +08:00 (§1.5)", tz_aware(b["timestamp"]),
          b["timestamp"])
    check("health: X-Request-Id ada (§1)", "x-request-id" in {k.lower() for k in r.headers},
          r.headers.get("x-request-id", "HILANG"))

    # ------------------------------------------------------------------
    # 4. GET /api/v1/forecast/actual — titik aktual untuk chart (§5.7)
    # ------------------------------------------------------------------
    r = client.get(
        "/api/v1/forecast/actual",
        params={
            "site_id": "PLTS_KUPANG",
            "from": "2024-03-01T00:00:00+08:00",
            "to": "2024-03-07T00:00:00+08:00",
        },
        headers=H,
    )
    check("GET /forecast/actual = 200", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        actual = r.json()
        spec_actual = {"site_id", "target", "unit", "timezone", "from", "to",
                       "source", "points", "n"}
        check("§5.7 semua field respons actual ada", spec_actual <= set(actual),
              f"hilang: {sorted(spec_actual - set(actual))}")
        check("§5.7 n == len(points) == 7",
              actual.get("n") == len(actual.get("points", [])) == 7,
              f"n={actual.get('n')}, points={len(actual.get('points', []))}")
        check("§5.7 unit W/m2 & timezone Asia/Makassar",
              actual.get("unit") == "W/m2"
              and actual.get("timezone") == "Asia/Makassar",
              f"{actual.get('unit')} / {actual.get('timezone')}")
        check("§5.7 setiap point punya timestamp + actual_value",
              all({"timestamp", "actual_value"} <= set(p)
                  for p in actual.get("points", [])))
        check("§5.7 timestamp aktual tz-aware +08:00",
              all(tz_aware(p["timestamp"]) and p["timestamp"].endswith("+08:00")
                  for p in actual.get("points", [])))
        check("§5.7 nilai aktual non-negatif",
              all(p["actual_value"] >= 0 for p in actual.get("points", [])))

    # ------------------------------------------------------------------
    # 4b. POST /api/v1/forecast — kontrak §5.1 + spesifikasi §2.1/§2.2
    # ------------------------------------------------------------------
    body = {
        "site_id": "PLTS_KUPANG",
        "target": "irradiance",
        "resolution": "daily",
        "horizon": 7,
        "history": history_payload(),
    }
    r = client.post("/api/v1/forecast", json=body, headers=H)
    check("POST /forecast = 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    f = r.json()

    # -- Spesifikasi §2.2 field wajib respons --
    spec22 = {"site_id", "ts", "generated_at", "resolution", "horizon", "unit",
              "timezone", "model_version", "forecast"}
    check("§2.2 semua field wajib respons ada", spec22 <= set(f),
          f"hilang: {sorted(spec22 - set(f))}")
    # `target` wajib per KONTRAK_API_v1.md §3.2 — hanya field yang
    # tidak tercantum di kedua dokumen yang dianggap asing.
    known = spec22 | {"order", "forecast_id", "target"}
    extra = sorted(set(f) - known)
    check("tidak ada field asing di luar kontrak §2.2 + §3.2", not extra, f"extra: {extra}")
    check("target ikut terlapor (kontrak §3.2, telusur model)",
          f.get("target") == "irradiance", str(f.get("target")))

    check("§2.2 site_id echo dari request", f["site_id"] == "PLTS_KUPANG", f["site_id"])
    check("§2.2 resolution = daily", f["resolution"] == "daily", f["resolution"])
    check("§2.2 horizon = 7", f["horizon"] == 7, str(f["horizon"]))
    check("§2.2 timezone = Asia/Makassar", f["timezone"] == "Asia/Makassar", f["timezone"])
    check("§2.2 unit = W/m2 (target irradiance)", f["unit"] == "W/m2", f["unit"])
    check("§2.2 ts tz-aware", tz_aware(f["ts"]), f["ts"])
    check("§2.2 generated_at tz-aware", tz_aware(f["generated_at"]), f["generated_at"])
    check("§2.2 generated_at TERPISAH dari ts (akurasi bisa diukur)",
          f["generated_at"] != f["ts"], f"ts={f['ts']} gen={f['generated_at']}")
    check("§2.2 model_version semver", bool(re.match(r"^v\d+\.\d+(\.\d+)?$", f["model_version"])),
          f["model_version"])
    check("§2.2 order tercantum (opsional)", "order" in f, str(f.get("order")))

    # -- §2.2 forecast[] --
    pts = f["forecast"]
    check("§2.2 panjang forecast == horizon", len(pts) == f["horizon"],
          f"{len(pts)} vs {f['horizon']}")
    check("§2.2 urutan kronologis naik & unik",
          all(pd.Timestamp(p["forecast_time"]) for p in pts) and
          [p["forecast_time"] for p in pts] == sorted(p["forecast_time"] for p in pts) and
          len({p["forecast_time"] for p in pts}) == len(pts))

    check("§2.1 tiap point punya forecast_time+forecast_value+model_version",
          all({"forecast_time", "forecast_value", "model_version"} <= set(p) for p in pts),
          str(list(pts[0])))
    check("§2.1 point tz-aware +08:00", all(tz_aware(p["forecast_time"]) for p in pts))
    check("§2.1 forecast_value 3 desimal",
          all(round(p["forecast_value"], 3) == p["forecast_value"] for p in pts),
          str([p["forecast_value"] for p in pts[:3]]))
    check("§2.1 forecast_value >= 0", all(p["forecast_value"] >= 0 for p in pts))
    check("§2.1 model_version per point konsisten dengan respons",
          all(p["model_version"] == f["model_version"] for p in pts))
    check("§2.1 forecast_time = ts + n hari (harian, step teratur)",
          (pd.Timestamp(pts[1]["forecast_time"]) - pd.Timestamp(pts[0]["forecast_time"])).days == 1,
          str(pd.Timestamp(pts[1]["forecast_time"]) - pd.Timestamp(pts[0]["forecast_time"])))

    # -- determinisme (dua POST identik -> forecast identik) --
    r2 = client.post("/api/v1/forecast", json=body, headers=H)
    f2 = r2.json()
    check("POST identik -> forecast identik (§5.1 deterministik)",
          [p["forecast_value"] for p in pts] == [p["forecast_value"] for p in f2["forecast"]])

    # ------------------------------------------------------------------
    # 5. POST tanpa key -> 401 (§6.1)
    # ------------------------------------------------------------------
    r = client.post("/api/v1/forecast", json=body)
    check("POST tanpa key = 401 E_UNAUTHORIZED",
          r.status_code == 401 and r.json()["error"]["code"] == "E_UNAUTHORIZED",
          f"{r.status_code} {r.json().get('error', {}).get('code')}")
    check("401 envelope punya request_id", r.json()["error"]["request_id"] == r.headers["x-request-id"])

    # ------------------------------------------------------------------
    # 6. Data kosong / horizon di luar batas (§6.1)
    # ------------------------------------------------------------------
    r = client.post("/api/v1/forecast", json={**body, "history": []}, headers=H)
    e = r.json()["error"]
    check("history kosong = 422 E_HISTORY_TOO_SHORT",
          r.status_code == 422 and e["code"] == "E_HISTORY_TOO_SHORT",
          f"{r.status_code} {e['code']}")
    check("422 membawa details[] terstruktur (§3.3)",
          bool(e.get("details")) and all({"field", "issue"} <= set(d) for d in e["details"]),
          str(e.get("details", [])[:1]))

    r = client.post("/api/v1/forecast", json={**body, "horizon": 31}, headers=H)
    e = r.json()["error"]
    check("horizon 31 = 422 E_HORIZON_RANGE",
          r.status_code == 422 and e["code"] == "E_HORIZON_RANGE",
          f"{r.status_code} {e['code']}")

    # ------------------------------------------------------------------
    # 7. GET /model/info (§5.4)
    # ------------------------------------------------------------------
    r = client.get("/api/v1/model/info", headers=H)
    check("GET /model/info = 200", r.status_code == 200, str(r.status_code))
    m = r.json()
    check("§5.4 model_version == versi file", m["model_version"] == f["model_version"],
          m["model_version"])
    check("§5.4 unit ikut terlapor", m.get("unit") == "W/m2", str(m.get("unit")))
    check("§5.4 features non-kosong (urutan fitur dari artefak)",
          isinstance(m.get("features"), list) and len(m["features"]) > 0,
          str(m.get("features")))
    check("§5.4 order = [1,0,2] (WITA)", m.get("order") == [1, 0, 2], str(m.get("order")))
    check("§5.4 evaluation_status enum", m.get("evaluation_status") in {"GO", "NO_GO", "UNKNOWN"},
          str(m.get("evaluation_status")))

    # ------------------------------------------------------------------
    # 8. GET /model/performance (§5.5)
    # ------------------------------------------------------------------
    r = client.get("/api/v1/model/performance", params={"site_id": "PLTS_KUPANG"}, headers=H)
    check("GET /model/performance = 200 (bukan 404)", r.status_code == 200, str(r.status_code))
    p = r.json()
    check("§5.5 membawa konteks site_id/unit/model_version",
          all(k in p for k in ("site_id", "unit", "model_version", "evaluated_at")))
    check("§5.5 status enum", p["status"] in {"GO", "NO_GO", "UNKNOWN"}, p["status"])
    check("§5.5 thresholds terkirim",
          isinstance(p.get("thresholds"), dict) and "max_mape" in p["thresholds"],
          str(p.get("thresholds")))
    # Konteks telusur (kontrak §5.5): evaluated_at harus waktu backtest asli
    # (bukan waktu request) dan membawa offset eksplisit; data_rows = n_rows
    # laporan, bukan n_samples.
    if p.get("evaluated_at"):
        check("§5.5 evaluated_at membawa offset eksplisit (§1.5)",
              tz_aware(p["evaluated_at"]), str(p["evaluated_at"]))
        report_json = Path("data/evaluation/report.json")
        if report_json.exists():
            report = json.loads(report_json.read_text(encoding="utf-8"))
            check("§5.5 evaluated_at == generated_at laporan (waktu backtest asli)",
                  str(p["evaluated_at"]).startswith(str(report.get("generated_at"))[:16]),
                  f"{p['evaluated_at']} vs {report.get('generated_at')}")
            check("§5.5 data_rows == n_rows laporan (bukan n_samples)",
                  p.get("data_rows") == report.get("n_rows"),
                  f"data_rows={p.get('data_rows')}, n_rows={report.get('n_rows')}, "
                  f"n_samples={p.get('n_samples')}")
    else:
        check("§5.5 evaluated_at terisi bila laporan ada", False, "null padahal laporan ada")

    # ------------------------------------------------------------------
    # 9. site_id tak dikenal (§6.1)
    # ------------------------------------------------------------------
    r = client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_NYASAR"}, headers=H)
    check("site tak dikenal = 404 E_SITE_UNKNOWN",
          r.status_code == 404 and r.json()["error"]["code"] == "E_SITE_UNKNOWN",
          f"{r.status_code} {r.json().get('error', {}).get('code')}")

    r = client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_KUPANG"}, headers=H)
    e = r.json().get("error", {})
    check("site valid + scheduler aktif = 200 dengan forecast tersimpan (bukan E_NO_FORECAST)",
          r.status_code == 200 and e.get("code") != "E_NO_FORECAST",
          f"{r.status_code} {e.get('code')}")
    if r.status_code == 200:
        latest = r.json()
        check("latest membawa forecast_id + konteks (kontrak §5.2)",
              {"forecast_id", "site_id", "generated_at", "unit", "model_version",
               "forecast"} <= set(latest), str(sorted(latest)))
        check("latest forecast_id dari scheduler (UUID, bukan null)",
              bool(latest.get("forecast_id")), str(latest.get("forecast_id")))

    # ------------------------------------------------------------------
    # 10. History pagination (§5.3)
    # ------------------------------------------------------------------
    r = client.get("/api/v1/forecast/history", params={"site_id": "PLTS_KUPANG"}, headers=H)
    check("GET /history = 200 dengan envelope page (§5.3)",
          r.status_code == 200 and "page" in r.json() and "items" in r.json(),
          str(list(r.json())))
    pg = r.json()["page"]
    check("page punya limit+has_more", {"limit", "has_more"} <= set(pg), str(pg))
    # Dengan scheduler aktif, history minimal berisi satu entri (tick startup);
    # has_more/next_cursor mengikuti jumlah entri vs limit. Toleran keduanya.
    items = r.json().get("items", [])
    check("history berisi >= 1 entri scheduler (kontrak §8)",
          isinstance(items, list) and len(items) >= 1, f"{len(items)} items")
    if pg["has_more"]:
        check("has_more=True -> next_cursor terisi", bool(pg.get("next_cursor")),
              str(pg.get("next_cursor")))
    else:
        check("has_more=False -> next_cursor hilang (bukan null)",
              "next_cursor" not in pg, str(pg))

    r = client.get("/api/v1/forecast/history",
                   params={"site_id": "PLTS_KUPANG", "cursor": "rusak"}, headers=H)
    check("cursor rusak = 400 E_CURSOR_INVALID",
          r.status_code == 400 and r.json()["error"]["code"] == "E_CURSOR_INVALID",
          f"{r.status_code} {r.json().get('error', {}).get('code')}")

    # ------------------------------------------------------------------
    # 11. Tanpa stack trace di semua jalur error
    # ------------------------------------------------------------------
    leak_markers = ["Traceback", 'File "', ".py\"", "Exception", "pydantic"]
    leaks = []
    for label, resp in [
        ("401", client.post("/api/v1/forecast", json=body)),
        ("404", client.get("/api/v1/forecast/latest", params={"site_id": "PLTS_X"}, headers=H)),
        ("422", client.post("/api/v1/forecast", json={**body, "horizon": 99}, headers=H)),
    ]:
        for mk in leak_markers:
            if mk in resp.text:
                leaks.append(f"{label}:{mk}")
    check("tidak ada stack trace di jalur error", not leaks, str(leaks))

    client.close()

    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("VALIDASI END-TO-END vs LEMBAR SPESIFIKASI (server live :8000)")
    print("=" * 78)
    width = max(len(l) for l, _, _ in RESULTS)
    fails = 0
    for label, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        line = f"  [{mark}] {label.ljust(width)}"
        if detail and not ok:
            line += f"  -> {detail}"
        print(line)

    print("\n" + "-" * 78)
    print(f"TOTAL: {len(RESULTS) - fails}/{len(RESULTS)} lulus")
    if fails:
        print(f"GAGAL: {fails}")
        return 1
    print("SEMUA FIELD COCOK DENGAN LEMBAR SPESIFIKASI — algoritma resmi jadi layanan web.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
