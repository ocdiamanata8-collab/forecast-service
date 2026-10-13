"""Uji isi image terhadap semua endpoint — tanpa Docker, tanpa async/port.

    python scripts/_image_sim_run.py

Pengganti jujur untuk `docker build && docker run && curl` bila Docker
absen: folder simulasi (scripts/_image_sim.py) berisi TEPAT file yang
di-COPY Dockerfile; aplikasi di-import dari folder itu — bukan dari repo —
sehingga semua path runtime dihitung relatif ke isi image.

Mengapa memakai TestClient (bukan uvicorn sungguhan): dua percobaan async
sebelumnya menggantung karena urllib memblokir event loop. TestClient
menjalankan lifespan & ASGI app secara in-process — pola yang sama dipakai
seluruh suite repo, jadi perilakunya identik dengan deploy.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

SIM = Path(tempfile.gettempdir()) / "forecast_svc_sim"
REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "sim-key-01234567890123456789"
H = {"X-API-Key": KEY}

# --- Import AKAN diambil dari folder simulasi, BUKAN repo -----------------
# Repo asli dikeluarkan dari sys.path agar `import app.main` menemukan
# app/ di SIM. Tanpa ini, tes bisa lolos dengan diam-diam memakai repo.
sys.path = [p for p in sys.path if Path(p).resolve() != REPO_ROOT]
sys.path.insert(0, str(SIM))
os.chdir(SIM)
os.environ["API_KEYS"] = KEY

import app.main as app_main  # noqa: E402

# Penjaga: pastikan modul benar-benar berasal dari folder simulasi.
_APP_ORIGIN = Path(app_main.__file__).resolve()
if SIM not in _APP_ORIGIN.parents:
    print(f"GAGAL: aplikasi ter-import dari {_APP_ORIGIN}, bukan dari {SIM}")
    raise SystemExit(1)

from fastapi.testclient import TestClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))


def sample_body() -> dict:
    import datetime as dt

    pts = []
    for i in range(60):
        ts = (dt.datetime(2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=8)))
              + dt.timedelta(days=i))
        v = max(0.0, 180 + 60 * math.sin(2 * math.pi * (i + 1) / 365.25))
        pts.append({"timestamp": ts.isoformat(), "value": round(v, 3)})
    return {"site_id": "PLTS_KUPANG", "target": "irradiance",
            "resolution": "daily", "horizon": 7, "history": pts}


def run_checks(c: TestClient) -> None:
    # 1. health — tanpa key, endpoint yang dipakai healthcheck container
    r = c.get("/api/v1/health")
    check("GET /health (tanpa key) = 200", r.status_code == 200, str(r.status_code))
    b = r.json()
    check("health: status dalam enum ok/degraded/unavailable",
          b.get("status") in {"ok", "degraded", "unavailable"}, str(b.get("status")))
    check("health: model_version TIDAK dibocorkan", "model_version" not in b)
    check("health: service_version + uptime ada",
          "service_version" in b and "uptime_seconds" in b)

    # 2. dashboard — membuktikan templates/ terbawa ke image
    r = c.get("/")
    check("GET / = 200 dan memuat dashboard",
          r.status_code == 200 and "Engineer Console" in r.text, str(r.status_code))

    # 3. docs — FastAPI jalan penuh di dalam isi image
    r = c.get("/docs")
    check("GET /docs = 200 + Swagger",
          r.status_code == 200 and "swagger" in r.text.lower(), str(r.status_code))

    # 4. openapi
    r = c.get("/openapi.json")
    n = len(r.json().get("paths", {}))
    check("OpenAPI = 7 endpoint", r.status_code == 200 and n == 7, f"n={n}")

    # 5. POST /forecast
    r = c.post("/api/v1/forecast", json=sample_body(), headers=H)
    check("POST /forecast = 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    if r.status_code == 200:
        d = r.json()
        check("POST /forecast: 7 titik", len(d.get("forecast", [])) == 7,
              str(len(d.get("forecast", []))))
        check("POST /forecast: konteks lengkap (site_id/generated_at/unit/model_version)",
              all(k in d for k in ("site_id", "generated_at", "unit", "model_version")))
        check("POST /forecast: forecast_time +08:00 (§1.5)",
              all(p["forecast_time"].endswith("+08:00") for p in d.get("forecast", [])))
        r2 = c.post("/api/v1/forecast", json=sample_body(), headers=H)
        # Kontrak §5.1: bandingkan forecast[] — BUKAN respons utuh.
        # generated_at sengaja berbeda tiap run (stempel pembuatan);
        # mengharapkannya sama adalah kesalahan aturan, bukan bug.
        check("POST /forecast: deterministik (forecast[] identik antar run)",
              r2.status_code == 200
              and r2.json().get("forecast") == d.get("forecast"))

    # 6. data actual — membuktikan data/ terbawa (dulu 503 kalau terlewat)
    # site_id WAJIB (kontrak §5.7). from/to sengaja di dalam cakupan
    # data 2023-2025 — default 60 hari terakhir jatuh di 2026 (luar
    # cakupan) sehingga n=0; itu perilaku benar §5.7, bukan bug.
    r = c.get(
        "/api/v1/forecast/actual",
        params={
            "site_id": "PLTS_KUPANG",
            "from": "2024-03-01T00:00:00+08:00",
            "to": "2024-03-07T00:00:00+08:00",
        },
        headers=H,
    )
    check("GET /forecast/actual = 200", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    if r.status_code == 200:
        check("/forecast/actual: ada titik (data/actuals.csv ikut image)",
              r.json().get("n", 0) > 0, f"n={r.json().get('n')}")

    # 7. model/performance — membuktikan data/evaluation/ terbawa
    r = c.get("/api/v1/model/performance", params={"site_id": "PLTS_KUPANG"}, headers=H)
    check("GET /model/performance = 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        d = r.json()
        check("performance: status enum + data_rows terkirim",
              d.get("status") in {"GO", "NO_GO", "UNKNOWN"} and "data_rows" in d,
              str(d.get("status")))

    # 8. tanpa key -> 401
    r = c.get("/api/v1/model/info")
    check("tanpa key -> 401 E_UNAUTHORIZED",
          r.status_code == 401 and r.json().get("error", {}).get("code") == "E_UNAUTHORIZED",
          str(r.status_code))

    # 9. health TETAP jalan saat model belum siap (gerbang deploy container)
    c.app.state.model_loaded = False
    r = c.get("/api/v1/health")
    check("model_loaded=False -> health tetap 200, status unavailable",
          r.status_code == 200 and r.json()["status"] == "unavailable",
          f"{r.status_code} {r.json().get('status') if r.status_code==200 else ''}")


def main() -> int:
    if not SIM.exists():
        print(f"GAGAL: folder simulasi belum ada ({SIM}). Jalankan: "
              "python scripts/_image_sim.py --prepare")
        return 1

    with TestClient(app_main.create_app()) as c:
        run_checks(c)

    print("\n" + "=" * 70)
    print("UJI ISI IMAGE (folder COPY + requirements, TANPA file di luar repo)")
    print(f"Origin modul: {_APP_ORIGIN}")
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
    print("APLIKASI JALAN PENUH DARI ISI IMAGE — TANPA FILE DI LUAR REPO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
