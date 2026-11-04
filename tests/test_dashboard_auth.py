"""Autentikasi dashboard engineer (HTTP Basic) — PoC sampai ada SSO.

    pytest tests/test_dashboard_auth.py -q

Yang dikunci:
- Tanpa kredensial → 401 + ``WWW-Authenticate`` (dialog login browser)
- Kredensial salah → 401 dengan envelope yang SAMA (tak ada oracle)
- Belum dikonfigurasi (env kosong) → 503, BUKAN terbuka
- API key TIDAK menggantikan login dashboard (dan sebaliknya)
- Percobaan login gagal dibatasi
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.dashboard_auth import LoginLimiter, get_login_limiter  # noqa: E402
from app.main import create_app  # noqa: E402

USER = os.environ.get("DASHBOARD_USER", "engineer")
PASSWORD = os.environ.get(
    "DASHBOARD_PASSWORD", "test-dashboard-password-0123456789"
)


def _basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + raw}


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    limiter = get_login_limiter()
    limiter._fails.clear()
    yield
    limiter._fails.clear()


def test_dashboard_requires_credentials():
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 401, response.status_code
        challenge = response.headers.get("www-authenticate", "")
        assert challenge.lower().startswith("basic"), challenge
        assert "Engineer Console" in challenge


def test_dashboard_served_with_correct_credentials():
    with TestClient(create_app()) as client:
        response = client.get("/", headers=_basic(USER, PASSWORD))
        assert response.status_code == 200, response.status_code
        assert response.headers["content-type"].startswith("text/html")
        # Isi dashboard tetap utuh (tidak boleh halaman login tipis).
        assert "Engineer Console" in response.text


def test_wrong_password_rejected_with_same_envelope():
    with TestClient(create_app()) as client:
        wrong = client.get("/", headers=_basic(USER, "salah-total"))
        assert wrong.status_code == 401
        body = wrong.json()
        assert body["error"]["code"] == "E_UNAUTHORIZED"
        # Username salah → pesan identik (tak ada oracle "user ada/tidak").
        unknown_user = client.get("/", headers=_basic("entah-siapa", PASSWORD))
        assert unknown_user.status_code == 401
        assert unknown_user.json()["error"]["message"] == body["error"]["message"]


def test_api_key_alone_does_not_unlock_dashboard():
    """X-API-Key bukan pengganti login dashboard (dua lapisan terpisah)."""
    api_key = os.environ.get("API_KEYS", "").split(",")[0].split(":", 1)[-1].strip()
    with TestClient(create_app()) as client:
        response = client.get("/", headers={"X-API-Key": api_key})
        assert response.status_code == 401, response.status_code


def test_dashboard_auth_not_configured_is_503_not_open(monkeypatch):
    """Tanpa DASHBOARD_USER/PASSWORD → 503 (deploy belum siap), bukan 200."""
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with TestClient(create_app()) as client:
        response = client.get("/", headers=_basic(USER, PASSWORD))
        assert response.status_code == 503, response.status_code
        assert response.json()["error"]["code"] == "E_SERVICE_UNAVAILABLE"


def test_repeated_failures_are_rate_limited():
    """Terlalu banyak percobaan gagal → 429 (anti tebak password)."""
    limiter = get_login_limiter()
    limiter.max_attempts = 3
    try:
        with TestClient(create_app()) as client:
            for _ in range(3):
                assert client.get("/", headers=_basic(USER, "salah")).status_code == 401
            blocked = client.get("/", headers=_basic(USER, "salah"))
            assert blocked.status_code == 429, blocked.status_code
            assert blocked.json()["error"]["code"] == "E_RATE_LIMITED"
    finally:
        limiter.max_attempts = LoginLimiter.max_attempts


def test_dashboard_user_env_is_logged_but_password_never():
    """Username boleh masuk log; password tidak boleh pernah."""
    from app.dashboard_auth import log_dashboard_auth_at_startup

    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger("app.dashboard_auth")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        log_dashboard_auth_at_startup()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    joined = " ".join(records)
    assert USER in joined, "username dashboard tidak tercatat di log startup"
    assert PASSWORD not in joined, "password dashboard bocor ke log!"
