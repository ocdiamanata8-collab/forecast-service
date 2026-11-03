"""Audit keamanan: semua endpoint data wajib API key; health saja terbuka.

Menginspeksi SEMUA route terdaftar di FastAPI (termasuk yang kelak ditambah),
bukan allowlist path statis. Endpoint internal include_in_schema=False juga
sengaja diperiksa terpisah.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def test_every_data_endpoint_requires_key_except_health():
    with TestClient(create_app()) as client:
        failures = []
        for route in client.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            for method in methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                if path == "/api/v1/health":
                    response = client.get(path)
                    assert response.status_code == 200
                    continue
                if not (path.startswith("/api/v1/") or path.startswith("/internal/v1/")):
                    continue
                if method == "GET":
                    # Minimal valid query defaults; unknown site validation is
                    # downstream of auth and must never precede the 401.
                    kwargs = {"params": {"site_id": "PLTS_KUPANG"}}
                    if path.endswith("/health"):
                        kwargs = {}
                    response = client.get(path, **kwargs)
                elif method == "POST":
                    payload = {"site_id": "PLTS_KUPANG"}
                    if path == "/api/v1/forecast":
                        payload.update({"horizon": 7, "history": []})
                    response = client.post(path, json=payload)
                else:
                    response = client.request(method, path, json={})
                if response.status_code != 401 or response.json().get("error", {}).get("code") != "E_UNAUTHORIZED":
                    failures.append(f"{method} {path} -> {response.status_code}: {response.text[:160]}")
        assert not failures, "Endpoint tanpa auth (kecuali health):\n" + "\n".join(failures)


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
