"""Uji adapter konsumen monitoring (static/monitoring-adapter.js).

    pytest tests/test_adapter.py -q

Adapter = SATU berkas titik adaptasi kontrak di sisi konsumen: response API
(nama field §5.x) → format internal platform ({when, value, …}). Tiga lapis
pengujian:

A. SERVE — berkas ter-serve di /static/monitoring-adapter.js (mount aktif,
   dan bukti file ikut ter-copy — kelas bug templates/ yang pernah terlewat).
B. PERILAKU — dijalankan via Node bila tersedia (skipif): pemetaan benar,
   angka numerik, urutan, dan TOLERANSI DRIFT (field kontrak diganti nama
   → tidak melempar, fallback null/[]). Node = runtime yang sama dengan
   browser (ESM) tanpa butuh browser.
C. GUARD dashboard — dashboard benar-benan meng-import adapter (bukan
   hiasan): <script type="module"> + import path adapter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

ADAPTER_PATH = "static/monitoring-adapter.js"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


# ---------------------------------------------------------------------------
# A. Serve
# ---------------------------------------------------------------------------
def test_adapter_is_served(client):
    r = client.get(f"/{ADAPTER_PATH}")
    assert r.status_code == 200, r.text[:200]
    assert "toForecast" in r.text and "toPerformance" in r.text
    # Mount /static tidak boleh membocorkan endpoint ke OpenAPI (bukan route).
    paths = client.get("/openapi.json").json()["paths"]
    assert "/static/{path:path}" not in paths
    assert "/static/monitoring-adapter.js" not in paths


# ---------------------------------------------------------------------------
# B. Perilaku via Node
# ---------------------------------------------------------------------------
def _node_available() -> bool:
    return shutil.which("node") is not None


NODE_TEST_JS = r"""
import assert from "node:assert/strict";
import { toForecast, toActualSeries, toPerformance, toModelInfo,
         toHealth, toHistory, toError } from "./adapter.mjs";

const results = [];
const check = (name, cond) => { results.push({ name, ok: !!cond }); };

// --- fixture payload kontrak (deterministik) ---
const forecastRaw = {
  site_id: "PLTS_KUPANG", target: "irradiance", resolution: "daily",
  unit: "W/m2", timezone: "Asia/Makassar",
  ts: "2025-12-31T00:00:00+08:00",
  generated_at: "2026-10-15T09:00:00+08:00",
  horizon: 3, model_version: "v1.0", order: "ARIMA(1, 0, 2)",
  forecast_id: "id-1",
  forecast: [
    { forecast_time: "2026-01-03T00:00:00+08:00", forecast_value: 221.8, model_version: "v1.0" },
    { forecast_time: "2026-01-01T00:00:00+08:00", forecast_value: 222.3, model_version: "v1.0" },
    { forecast_time: "2026-01-02T00:00:00+08:00", forecast_value: 221.9, model_version: "v1.0" },
  ],
};
const m = toForecast(forecastRaw);
check("forecast: site", m.site === "PLTS_KUPANG");
check("forecast: generatedAt", m.generatedAt === "2026-10-15T09:00:00+08:00");
check("forecast: points terurut", m.points.map(p => p.when).join() ===
  "2026-01-01T00:00:00+08:00,2026-01-02T00:00:00+08:00,2026-01-03T00:00:00+08:00");
check("forecast: value numerik", m.points.every(p => typeof p.value === "number"));
check("forecast: modelVersion per poin", m.points[0].modelVersion === "v1.0");

const actualRaw = {
  site_id: "PLTS_KUPANG", target: "irradiance", unit: "W/m2",
  timezone: "Asia/Makassar", from: "2024-03-01T00:00:00+08:00",
  to: "2024-03-02T00:00:00+08:00", source: "data/actuals.csv", n: 2,
  points: [
    { timestamp: "2024-03-02T00:00:00+08:00", actual_value: 250.972 },
    { timestamp: "2024-03-01T00:00:00+08:00", actual_value: 199.226 },
  ],
};
const a = toActualSeries(actualRaw);
check("actual: points numerik terurut",
  a.points[0].value === 199.226 && a.points[1].when.startsWith("2024-03-02"));

const perfRaw = {
  site_id: "PLTS_KUPANG", target: "irradiance", resolution: "daily", unit: "W/m2",
  model_version: "v1.0", evaluated_at: "2026-10-14T12:25:00+08:00",
  data_rows: 1096, n_samples: 336, status: "GO", failures: [],
  thresholds: { max_mae: 45.0, max_mape: 28.0, max_mape_long: 33.0,
                min_skill: 0.0, primary_horizon: 7 },
  evaluation_range: { from: "2025-01-01T00:00:00+08:00", to: "2025-12-31T00:00:00+08:00" },
  horizons: [
    { horizon: 1, mae: 40.513, rmse: 50.4, mape: 25.69, skill: 0.291 },
    { horizon: 7, mae: 40.255, rmse: 50.022, mape: 25.56, skill: 0.244 },
  ],
};
const p = toPerformance(perfRaw);
check("performance: status", p.status === "GO");
check("performance: primary = horizon 7", p.primary !== null && p.primary.days === 7 && p.primary.mape === 25.56);
check("performance: thresholds terjemah", p.thresholds.maxMape === 28.0 && p.thresholds.primaryHorizon === 7);
check("performance: dataRows", p.dataRows === 1096);

const info = toModelInfo({
  model_version: "v1.0", target: "irradiance", unit: "W/m2", algorithm: "ARIMA",
  order: [1, 0, 2], timezone: "Asia/Makassar", features: ["temperature", "tcc"],
  training_rows: 1096, trained_at: "2026-10-02T00:00:00+08:00",
  evaluation_status: "GO", deterministic: true,
  training_range: { from: "2023-01-01T00:00:00+08:00", to: "2025-12-31T00:00:00+08:00" },
});
check("info: features", info.features.join() === "temperature,tcc");
check("info: trainingRows", info.trainingRows === 1096);

const h = toHealth({ status: "ok", service_version: "v1.0",
                     timestamp: "2026-10-15T09:00:00+08:00",
                     uptime_seconds: 120, model_loaded: true });
check("health: uptime numerik", h.uptimeSeconds === 120 && h.status === "ok");

const hist = toHistory({
  items: [
    { forecast_id: "b", site_id: "PLTS_KUPANG", generated_at: "2026-10-15T09:00:00+08:00",
      ts: "2025-12-31T00:00:00+08:00", horizon: 7, model_version: "v1.0" },
    { forecast_id: "a", site_id: "PLTS_KUPANG", generated_at: "2026-10-14T09:00:00+08:00",
      ts: "2025-12-30T00:00:00+08:00", horizon: 7, model_version: "v1.0" },
  ],
  page: { limit: 20, has_more: false },
});
check("history: urut generated_at desc", hist.items[0].forecastId === "b");
check("history: page + nextCursor null", hist.page.hasMore === false && hist.page.nextCursor === null);

const err = toError({ error: { code: "E_NO_FORECAST", message: "x",
                     request_id: "rid-1" } }, 404);
check("error: envelope", err.status === 404 && err.code === "E_NO_FORECAST" && err.requestId === "rid-1");

// --- TOLERANSI DRIFT: field kontrak diganti nama → TIDAK melempar ---
let threw = false;
try {
  const drift = toForecast({ site_id: "X", forecast_v2: [{ x: 1 }] });
  check("drift: points fallback []", Array.isArray(drift.points) && drift.points.length === 0);
  check("drift: site tetap terbaca", drift.site === "X");
  check("drift: generatedAt null", drift.generatedAt === null);
  const driftP = toPerformance({ status: "GO" });
  check("drift: performance fallback", driftP.primary === null && driftP.thresholds.maxMape === null);
  const driftH = toHealth({});
  check("drift: health fallback", driftH.status === null && driftH.uptimeSeconds === null);
} catch (e) { threw = true; }
check("drift: TIDAK melempar exception", !threw);

console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(not _node_available(), reason="node tidak tersedia di mesin ini")
def test_adapter_behavior_via_node(tmp_path):
    """Perilaku penerjemahan diuji di Node — ESM yang sama dengan browser."""
    shutil.copy(Path(__file__).resolve().parents[1] / ADAPTER_PATH,
                tmp_path / "adapter.mjs")
    script = tmp_path / "test.mjs"
    script.write_text(NODE_TEST_JS, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"node gagal: {proc.stderr[:500]}"
    import json

    results = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [r["name"] for r in results if not r["ok"]]
    assert not failed, f"assertion adapter gagal: {failed} (node stderr: {proc.stderr[:200]})"


# ---------------------------------------------------------------------------
# C. Guard dashboard — benar-benar memakai adapter
# ---------------------------------------------------------------------------
def test_dashboard_imports_adapter(client):
    html = client.get("/").text
    assert 'type="module"' in html, "dashboard harus memakai <script type=module>"
    assert ADAPTER_PATH in html, "dashboard harus meng-import adapter"
    assert "toForecast" in html and "toPerformance" in html and "toHealth" in html


# ---------------------------------------------------------------------------
# Mode script
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
