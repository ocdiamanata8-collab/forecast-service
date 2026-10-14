"""Uji pemisahan LOGIKA BISNIS dari lapisan HTTP.

    pytest tests/test_service_layer.py -q

Inti instruksi: "kalau ada logika di endpoint, ia akan terkunci di HTTP
layer dan tidak bisa dipakai ulang oleh scheduler di Fase 4."

Test ini membuktikan tiga hal, dan [1]-[2] adalah guard permanen terhadap
kemunduran arsitektur:

[1] ``app/service.py`` TIDAK mengimpor ``fastapi`` maupun ``pydantic``.
    Bila suatu hari seseorang menambah logika bisnis ke endpoint lalu
    menariknya ke service dengan ikut mengimpor FastAPI, guard ini gagal.
[2] Scheduler dipanggil TANPA HTTP: tanpa TestClient, tanpa payload JSON,
    tanpa header API key — cukup tipe polos.
[3] Hasil service == hasil endpoint untuk input identik (tidak ada logika
    yang terlewat saat dipindah).
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.main import create_app  # noqa: E402

FAILURES: list[str] = []
DEMO_KEY = "dev-key-change-me-0123456789"
ROOT = Path(__file__).resolve().parents[1]
# Set API key di environment SEBELUM lifespan berjalan (tidak ada fallback
# di app/main.py). conftest.py hanya menangani mode pytest; script mode
# perlu mengaturnya sendiri.
os.environ.setdefault("API_KEYS", DEMO_KEY)
os.environ.setdefault("SCHEDULER_ENABLED", "false")  # state test deterministik


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def scheduler_history(n: int = 90) -> list[tuple[pd.Timestamp, float]]:
    """Data polos persis seperti yang dipegang scheduler — tanpa Pydantic."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="Asia/Makassar")
    doy = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * doy / 365.25), 0, None)
    return list(zip(list(idx), values.tolist()))


# ===========================================================================
# pytest — guard arsitektur
# ===========================================================================
def test_service_does_not_import_web_layer():
    """Guard [1]: service bebas fastapi/pydantic.

    Inilah yang membuatnya dipakai scheduler. Kalau test ini gagal, logika
    bisnis mulai menarik dependensi web ke dalam dirinya.
    """
    source = (ROOT / "app" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    web = imported & {"fastapi", "pydantic", "starlette", "httpx"}
    assert not web, (
        f"app/service.py mengimpor lapisan web {sorted(web)} — logika bisnis "
        "terkunci di HTTP layer dan tidak terpakai scheduler"
    )


def test_service_module_has_no_route_decorators():
    """Guard [1b]: tidak ada ``@router`` / ``@app.get`` di service."""
    source = (ROOT / "app" / "service.py").read_text(encoding="utf-8")
    for marker in ("@router", "@app.get", "@app.post", "APIRouter", "Depends("):
        assert marker not in source, f"penanda HTTP ditemukan di service: {marker}"


def test_scheduler_runs_forecast_without_http():
    """Guard [2]: dipanggil polos, tanpa TestClient / JSON / header."""
    outcome = service.run_forecast(
        site_id="PLTS_KUPANG",
        history=scheduler_history(),
        horizon=7,
    )
    assert len(outcome.points) == 7
    assert outcome.unit == "W/m2"
    assert outcome.timezone == "Asia/Makassar"
    assert outcome.site_id == "PLTS_KUPANG"
    assert outcome.generated_at.tzinfo is not None
    assert all(p.forecast_value >= 0 for p in outcome.points)
    assert all(round(p.forecast_value, 3) == p.forecast_value for p in outcome.points)


def test_scheduler_injects_generated_at():
    """Scheduler mengatur waktu pembuatan sendiri (rolling forecast per jam)."""
    from datetime import datetime, timezone

    fixed = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    out_a = service.run_forecast(
        site_id="PLTS_KUPANG", history=scheduler_history(), horizon=3,
        generated_at=fixed,
    )
    out_b = service.run_forecast(
        site_id="PLTS_KUPANG", history=scheduler_history(), horizon=3,
        generated_at=fixed,
    )
    assert out_a.generated_at == out_b.generated_at
    # Timestamp identik → forecast identik (determinisme §5.1).
    assert [p.forecast_value for p in out_a.points] == \
        [p.forecast_value for p in out_b.points]


def test_service_reports_contract_error_codes():
    """``ForecastError.code`` membawa kode kontrak — scheduler bisa memakainya."""
    with pytest.raises(service.ForecastError) as exc:
        service.run_forecast(site_id="PLTS_KUPANG",
                             history=scheduler_history()[:5], horizon=7)
    assert exc.value.code in {"E_VALIDATION_FAILED", "E_HISTORY_TOO_SHORT",
                              "E_TZ_REQUIRED", "E_IRREGULAR_SERIES"}
    assert exc.value.status == 422


def test_service_rejects_naive_timestamps():
    naive = [(pd.Timestamp(ts).tz_localize(None), v)
             for ts, v in scheduler_history()[:40]]
    try:
        service.run_forecast(site_id="PLTS_KUPANG", history=naive, horizon=3)
        raised = False
    except service.ForecastError:
        raised = True
    except Exception as exc:  # pandas naikkan error timezone-naif
        raised = "tz" in str(exc).lower() or "timezone" in str(exc).lower()
    assert raised, "timestamp naive harus ditolak di level service"


def test_endpoint_and_service_produce_identical_result():
    """Guard [3]: tidak ada logika yang terlewat saat dipindah."""
    history = scheduler_history()
    outcome = service.run_forecast(
        site_id="PLTS_KUPANG", history=history, horizon=7
    )

    body = {
        "site_id": "PLTS_KUPANG",
        "target": "irradiance",
        "resolution": "daily",
        "horizon": 7,
        "history": [
            {"timestamp": ts.isoformat(), "value": float(v)}
            for ts, v in history
        ],
    }
    with TestClient(create_app()) as client:
        r = client.post("/api/v1/forecast", json=body,
                        headers={"X-API-Key": DEMO_KEY})
        assert r.status_code == 200, r.text[:400]
        via_http = r.json()

    via_service = [
        {
            "forecast_time": p.forecast_time.isoformat(),
            "forecast_value": p.forecast_value,
            "model_version": p.model_version,
        }
        for p in outcome.points
    ]
    assert [f["forecast_value"] for f in via_http["forecast"]] == \
        [p["forecast_value"] for p in via_service], "angka berbeda antara service dan endpoint"
    assert via_http["unit"] == outcome.unit
    assert via_http["order"] == outcome.order
    assert via_http["model_version"] == outcome.model_version
    assert via_http["ts"] == outcome.ts.isoformat()


# ===========================================================================
# Mode script
# ===========================================================================
def main() -> int:
    print("\n[1] SERVICE BEBAS LAPISAN WEB")
    source = (ROOT / "app" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    web = imported & {"fastapi", "pydantic", "starlette", "httpx"}
    check(not web, f"tanpa fastapi/pydantic (ditemukan: {sorted(web)})")
    check("APIRouter" not in source and "@router" not in source,
          "tanpa dekorator route")

    print("\n[2] SCHEDULER MEMANGGIL TANPA HTTP")
    outcome = service.run_forecast(
        site_id="PLTS_KUPANG", history=scheduler_history(), horizon=7
    )
    check(len(outcome.points) == 7, f"{len(outcome.points)} poin forecast")
    check(outcome.unit == "W/m2", f"unit={outcome.unit}")
    check(str(outcome.generated_at.tzinfo) == "Asia/Makassar",
          f"generated_at zona site ({outcome.generated_at})")
    check(all(round(p.forecast_value, 3) == p.forecast_value for p in outcome.points),
          "pembulatan 3 desimal (§3.1)")
    check(all(p.forecast_value >= 0 for p in outcome.points), "nilai >= 0 (C-05)")

    print("\n[3] ENDPOINT DAN SERVICE IDENTIK")
    history = scheduler_history()
    out = service.run_forecast(site_id="PLTS_KUPANG", history=history, horizon=7)
    body = {
        "site_id": "PLTS_KUPANG", "target": "irradiance",
        "resolution": "daily", "horizon": 7,
        "history": [{"timestamp": t.isoformat(), "value": float(v)} for t, v in history],
    }
    with TestClient(create_app()) as client:
        r = client.post("/api/v1/forecast", json=body,
                        headers={"X-API-Key": DEMO_KEY})
    check(r.status_code == 200, f"endpoint 200 (dapat {r.status_code})")
    if r.status_code == 200:
        http_vals = [f["forecast_value"] for f in r.json()["forecast"]]
        svc_vals = [p.forecast_value for p in out.points]
        check(http_vals == svc_vals, "angka identik service vs endpoint")

    print("\n[4] SCHEDULER-SIDE: galat domain berkode kontrak")
    try:
        service.run_forecast(site_id="PLTS_KUPANG",
                             history=scheduler_history()[:5], horizon=7)
        check(False, "history pendek harus ditolak")
    except service.ForecastError as exc:
        check(exc.status == 422, f"code={exc.code} status={exc.status}")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("LOGIKA BISNIS BEBAS DARI LAPISAN HTTP — siap untuk scheduler")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
