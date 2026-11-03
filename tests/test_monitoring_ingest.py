"""Uji modul ingesta platform monitoring (static/monitoring-ingest.js).

Fase B checklist — fetch + adapter + cache per site.

    pytest tests/test_monitoring_ingest.py -q
"""

from __future__ import annotations

import json
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

INGEST_PATH = "static/monitoring-ingest.js"
ADAPTER_PATH = "static/monitoring-adapter.js"


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def test_ingest_module_is_served(client):
    r = client.get(f"/{INGEST_PATH}")
    assert r.status_code == 200, r.text[:200]
    assert "ingestMonitoringSite" in r.text
    assert "MonitoringSiteCache" in r.text
    assert "monitoring-adapter.js" in r.text


def _node_available() -> bool:
    return shutil.which("node") is not None


NODE_TEST_JS = r"""
import assert from "node:assert/strict";
import {
  ingestMonitoringSite,
  MonitoringSiteCache,
  clampCacheTtlMs,
  joinBaseUrl,
} from "./ingest.mjs";

const results = [];
const check = (name, cond) => { results.push({ name, ok: !!cond }); };

const forecastPayload = {
  site_id: "PLTS_KUPANG",
  ts: "2025-12-31T00:00:00+08:00",
  generated_at: "2026-10-15T09:00:00+08:00",
  horizon: 2,
  model_version: "v1.0",
  forecast: [
    { forecast_time: "2025-12-31T00:00:00+08:00", forecast_value: 99, model_version: "v1.0" },
    { forecast_time: "2026-01-01T00:00:00+08:00", forecast_value: 100, model_version: "v1.0" },
    { forecast_time: "2026-01-02T00:00:00+08:00", forecast_value: 101, model_version: "v1.0" },
  ],
};

const historyPayload = {
  items: [
    {
      forecast_id: "h1",
      site_id: "PLTS_KUPANG",
      generated_at: "2026-10-14T09:00:00+08:00",
      ts: "2025-12-30T00:00:00+08:00",
      horizon: 7,
      model_version: "v1.0",
    },
  ],
  page: { limit: 50, has_more: false },
};

const perfPayload = {
  site_id: "PLTS_KUPANG",
  model_version: "v1.0",
  status: "GO",
  evaluated_at: "2026-10-14T12:00:00+08:00",
  thresholds: { primary_horizon: 7, max_mape: 28 },
  horizons: [{ horizon: 7, mae: 40, rmse: 50, mape: 25, skill: 0.2 }],
};

const healthPayload = {
  status: "ok",
  service_version: "v1.0",
  timestamp: "2026-10-15T09:00:00+08:00",
  uptime_seconds: 10,
  model_loaded: true,
};

let call = 0;
async function mockFetch(url, init = {}) {
  call += 1;
  const u = String(url);
  const hasKey = init.headers && init.headers["X-API-Key"] === "secret-key";
  if (u.includes("/health")) {
    check("health: tanpa key", !hasKey);
    return { status: 200, json: async () => healthPayload, headers: new Headers() };
  }
  check("auth: key pada endpoint terproteksi", hasKey);
  if (u.includes("/forecast/latest")) {
    return { status: 200, json: async () => forecastPayload, headers: new Headers() };
  }
  if (u.includes("/forecast/history")) {
    check("history: limit=50", u.includes("limit=50"));
    return { status: 200, json: async () => historyPayload, headers: new Headers() };
  }
  if (u.includes("/model/performance")) {
    return { status: 200, json: async () => perfPayload, headers: new Headers() };
  }
  return { status: 404, json: async () => ({ error: { code: "E_NOT_FOUND" } }), headers: new Headers() };
}

const bundle = await ingestMonitoringSite({
  baseUrl: "https://example.test",
  siteId: "PLTS_KUPANG",
  apiKey: "secret-key",
  fetchImpl: mockFetch,
});

check("bundle: siteId", bundle.siteId === "PLTS_KUPANG");
check("bundle: generatedAt dari latest", bundle.generatedAt === "2026-10-15T09:00:00+08:00");
check("bundle: modelVersion", bundle.modelVersion === "v1.0");
check("bundle: forecast normalized", bundle.latest.forecast.points.length === 3);
check("bundle: inHorizon dari adapter", bundle.latest.forecast.points.some(p => p.inHorizon === false));
check("bundle: validUntil dari adapter", bundle.latest.forecast.validUntil === "2026-01-02");
check("bundle: performance GO", bundle.performance.performance.status === "GO");
check("bundle: history items", bundle.history.history.items.length === 1);
check("bundle: health ok", bundle.health.health.status === "ok");
check("fetch: empat endpoint", call === 4);

// cache: fresh hit
const cache = new MonitoringSiteCache({ ttlMs: 600_000, revalidateInBackground: false });
let fetches = 0;
async function countingFetch(url, init) {
  fetches += 1;
  return mockFetch(url, init);
}
const opts = {
  baseUrl: "https://example.test",
  apiKey: "secret-key",
  fetchImpl: countingFetch,
};
const first = await cache.load("PLTS_KUPANG", opts);
check("cache: miss pertama", !first.fromCache && !first.stale);
const second = await cache.load("PLTS_KUPANG", opts);
check("cache: hit fresh", second.fromCache && !second.stale);
check("cache: satu round-trip", fetches === 4);

check("clamp ttl", clampCacheTtlMs(1000) === 300000 && clampCacheTtlMs(9999999) === 900000);
check("join url", joinBaseUrl("https://x/", "/api/v1/health") === "https://x/api/v1/health");

console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(not _node_available(), reason="node tidak tersedia di mesin ini")
def test_ingest_behavior_via_node(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / ADAPTER_PATH, tmp_path / "monitoring-adapter.js")
    shutil.copy(root / INGEST_PATH, tmp_path / "ingest.mjs")
    script = tmp_path / "run.mjs"
    script.write_text(NODE_TEST_JS, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"node gagal: {proc.stderr[:800]}"
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [r["name"] for r in results if not r["ok"]]
    assert not failed, failed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
