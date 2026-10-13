"""Uji endpoint health (kontrak §5.6) — jalur yang dipakai uptime monitor & deploy gate.

    pytest tests/test_health.py -q
    python tests/test_health.py

Yang divalidasi:

1. Terbuka tanpa ``X-API-Key`` (monitor eksternal tidak punya key).
2. ``status`` satu dari tiga nilai kontrak — ``ok`` / ``degraded`` /
   ``unavailable``. Sketsa ``"online"`` TIDAK sah: monitor butuh tiga
   kondisi, bukan sakelar hidup/mati.
3. ``model_version`` TIDAK muncul di health publik (kontrak §1.1) — detail
   model ada di ``/model/info`` yang butuh key. Field di sini
   ``service_version``.
4. ``service_version`` == baris pertama ``model/VERSION`` (satu sumber benar).
5. Artefak/versi rusak → ``degraded`` (bukan ``ok``) — inilah gunanya
   sebagai gerbang deploy.
6. ``uptime_seconds`` ≥ 0 dan ``timestamp`` offset ``+08:00`` (§1.5).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.artifacts import ArtifactError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.version import model_version  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


CONTRACT_STATUSES = {"ok", "degraded", "unavailable"}


# ---------------------------------------------------------------------------
# Test pytest
# ---------------------------------------------------------------------------
# Catatan: create_app() mengembalikan FastAPI (bukan context manager).
# Lifespan dijalankan oleh TestClient(...) — jangan pakai `with create_app()`.
# ---------------------------------------------------------------------------
def test_health_is_open_without_api_key():
    """Monitor uptime eksternal tidak membawa key — health harus terbuka."""
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        # Tanpa header sama sekali: tidak ada 401/403.
        assert "X-API-Key" not in response.headers
        body = response.json()
        assert body["status"] in CONTRACT_STATUSES


def test_health_status_contract_enum():
    """``status`` harus salah satu dari tiga nilai kontrak, bukan 'online'."""
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        body = client.get("/api/v1/health").json()
        assert body["status"] in CONTRACT_STATUSES, body["status"]
        assert body["status"] != "online", (
            "'online' bukan nilai kontrak §5.6 — monitor butuh ok/degraded/unavailable"
        )


def test_health_exposes_service_version_not_model_version():
    """§5.6: health publik TIDAK membocorkan ``model_version`` (tanpa key)."""
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        body = client.get("/api/v1/health").json()
        assert "model_version" not in body, (
            "model_version bocor di endpoint tanpa autentikasi (kontrak §1.1)"
        )
        assert "service_version" in body
        # Field yang diizinkan kontrak saja.
        allowed = {"status", "service_version", "timestamp",
                   "uptime_seconds", "model_loaded"}
        assert set(body) == allowed, set(body)


def test_health_service_version_matches_version_file():
    """Satu sumber kebenaran: service_version == baris pertama model/VERSION."""
    from fastapi.testclient import TestClient

    version_file = (Path(__file__).resolve().parents[1] / "model" / "VERSION")
    expected = version_file.read_text(encoding="utf-8").splitlines()[0].strip()
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/health").json()
        assert body["service_version"] == expected == model_version()


def test_health_time_and_uptime_shape():
    """§1.5 & §5.6: timestamp offset eksplisit zona site; uptime ≥ 0."""
    import re

    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        body = client.get("/api/v1/health").json()
        assert re.search(r"[+-]08:00$", body["timestamp"]), body["timestamp"]
        assert body["uptime_seconds"] >= 0
        assert isinstance(body["model_loaded"], bool)


def test_health_degraded_when_artifacts_unreadable():
    """Artefak model rusak → ``degraded``. Gerbang deploy justru di sini."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    def boom() -> object:
        raise ArtifactError("artefak model tidak ditemukan (uji)")

    # Patch pembaca artefak SEBELUM lifespan berjalan, supaya artifacts_ok=False.
    with patch("app.main.load_artifacts", side_effect=boom):
        with TestClient(create_app()) as client:
            body = client.get("/api/v1/health").json()
            assert body["status"] == "degraded", (
                f"artefak gagal dibaca harus degraded, dapat {body['status']}"
            )
            # Service tetap menjawab (bukan 503) — tapi JANGAN dilaporkan siap.
            assert body["model_loaded"] is True


def test_health_reports_unavailable_when_model_not_loaded():
    """``model_loaded`` false → ``unavailable`` (deploy gate menahan traffic)."""
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        # Paksa state setelah lifespan selesai (simulasi model belum termuat).
        client.app.state.model_loaded = False
        body = client.get("/api/v1/health").json()
        assert body["status"] == "unavailable", body
        # Uptime monitor akan menahan traffic — itu tujuannya.
        assert body["model_loaded"] is False


def test_health_response_has_request_id_header():
    """§1: header ``X-Request-Id`` pada SEMUA respons, termasuk health."""
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")
        assert "X-Request-Id" in response.headers
        assert len(response.headers["X-Request-Id"]) >= 8


# ---------------------------------------------------------------------------
# Mode script (jalankan langsung dengan python)
# ---------------------------------------------------------------------------
def main() -> int:
    from fastapi.testclient import TestClient

    print("\n[1] ENDPOINT TERBUKA TANPA KEY")
    with TestClient(create_app()) as client:
        r = client.get("/api/v1/health")
        check(r.status_code == 200, f"GET /api/v1/health -> {r.status_code}")
        body = r.json()
        check("model_version" not in body, "model_version TIDAK bocor di health publik")
        check(body["status"] in CONTRACT_STATUSES,
              f"status dalam enum kontrak ({body['status']})")
        check(body["service_version"] == model_version(),
              f"service_version == model/VERSION ({body['service_version']})")
        check(body["uptime_seconds"] >= 0, f"uptime={body['uptime_seconds']}s")
        check(str(body["timestamp"]).endswith("+08:00"),
              f"timestamp zona site ({body['timestamp']})")
        check("X-Request-Id" in r.headers, "header X-Request-Id ada")
        check(set(body) == {"status", "service_version", "timestamp",
                            "uptime_seconds", "model_loaded"},
              "field persis daftar kontrak §5.6")

    print("\n[2] MODEL BELUM TERMUAT -> unavailable")
    with TestClient(create_app()) as client:
        client.app.state.model_loaded = False
        b = client.get("/api/v1/health").json()
        check(b["status"] == "unavailable", f"status={b['status']}")

    print("\n[3] ARTEFAK RUSAK -> degraded (gerbang deploy)")
    from unittest.mock import patch

    def boom():
        raise ArtifactError("artefak model tidak ditemukan (uji)")

    with patch("app.main.load_artifacts", side_effect=boom):
        with TestClient(create_app()) as client:
            b = client.get("/api/v1/health").json()
            check(b["status"] == "degraded",
                  f"status saat artefak gagal = {b['status']}")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("HEALTH ENDPOINT SESUAI KONTRAK §5.6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
