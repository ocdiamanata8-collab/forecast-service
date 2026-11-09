"""CORS terbatas (ALLOWED_ORIGINS) dan paksa HTTPS (FORCE_HTTPS)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("FORCE_HTTPS", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.http_policy import parse_allowed_origins  # noqa: E402
from app.main import create_app  # noqa: E402


def test_parse_allowed_origins_rejects_wildcard_and_spaces():
    assert parse_allowed_origins("") == []
    assert parse_allowed_origins("https://monitoring.sei.co.id") == [
        "https://monitoring.sei.co.id"
    ]
    with pytest.raises(ValueError, match=r"\*"):
        parse_allowed_origins("*")
    with pytest.raises(ValueError, match="spasi"):
        parse_allowed_origins("https://a.example, https://b.example")


def test_cors_allows_configured_origin_on_health():
    prev = os.environ.get("ALLOWED_ORIGINS")
    os.environ["ALLOWED_ORIGINS"] = "https://monitoring.sei.co.id"
    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/health",
                headers={"Origin": "https://monitoring.sei.co.id"},
            )
            assert response.status_code == 200
            assert (
                response.headers.get("access-control-allow-origin")
                == "https://monitoring.sei.co.id"
            )
    finally:
        if prev is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev


def test_cors_blocks_unknown_origin():
    prev = os.environ.get("ALLOWED_ORIGINS")
    os.environ["ALLOWED_ORIGINS"] = "https://monitoring.sei.co.id"
    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/health",
                headers={"Origin": "https://evil.example"},
            )
            assert response.status_code == 200
            assert "access-control-allow-origin" not in response.headers
    finally:
        if prev is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev


def test_cors_preflight_allows_x_api_key():
    prev = os.environ.get("ALLOWED_ORIGINS")
    os.environ["ALLOWED_ORIGINS"] = "https://monitoring.sei.co.id"
    try:
        with TestClient(create_app()) as client:
            response = client.options(
                "/api/v1/forecast/latest",
                headers={
                    "Origin": "https://monitoring.sei.co.id",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-api-key",
                },
            )
            assert response.status_code == 200
            allow_headers = response.headers.get(
                "access-control-allow-headers", ""
            ).lower()
            assert "x-api-key" in allow_headers
            assert (
                response.headers.get("access-control-allow-origin")
                == "https://monitoring.sei.co.id"
            )
    finally:
        if prev is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev


def test_force_https_redirects_http_request():
    prev_force = os.environ.get("FORCE_HTTPS")
    prev_origins = os.environ.get("ALLOWED_ORIGINS")
    os.environ["FORCE_HTTPS"] = "true"
    os.environ.pop("ALLOWED_ORIGINS", None)
    try:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/health", follow_redirects=False)
            assert response.status_code in {301, 307, 308}
            assert response.headers.get("location", "").startswith("https://")
    finally:
        if prev_force is None:
            os.environ.pop("FORCE_HTTPS", None)
        else:
            os.environ["FORCE_HTTPS"] = prev_force
        if prev_origins is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev_origins


def test_proxy_forwarded_https_does_not_redirect_loop():
    """Regresi: proxy TLS (Blitz/Render) + ``X-Forwarded-Proto: https``.

    Kalau ProxyHeaders berada DI DALAM HTTPSRedirect, scope scheme masih
    ``http`` saat redirect dievaluasi → 307 ke URL https yang sama → browser
    mengulang selamanya (ERR_TOO_MANY_REDIRECTS).
    """
    prev_force = os.environ.get("FORCE_HTTPS")
    prev_origins = os.environ.get("ALLOWED_ORIGINS")
    os.environ["FORCE_HTTPS"] = "true"
    os.environ.pop("ALLOWED_ORIGINS", None)
    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/health",
                headers={"X-Forwarded-Proto": "https"},
                follow_redirects=False,
            )
            assert response.status_code == 200
            assert "location" not in response.headers

            # Setelah ikut redirect pun harus mendarat, bukan redirect lagi.
            followed = client.get(
                "/api/v1/health",
                headers={"X-Forwarded-Proto": "https"},
                follow_redirects=True,
            )
            assert followed.status_code == 200
    finally:
        if prev_force is None:
            os.environ.pop("FORCE_HTTPS", None)
        else:
            os.environ["FORCE_HTTPS"] = prev_force
        if prev_origins is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev_origins


def test_proxy_forwarded_http_still_redirects_once():
    """Proxy yang mengirim ``X-Forwarded-Proto: http`` → redirect ke https sekali."""
    prev_force = os.environ.get("FORCE_HTTPS")
    prev_origins = os.environ.get("ALLOWED_ORIGINS")
    os.environ["FORCE_HTTPS"] = "true"
    os.environ.pop("ALLOWED_ORIGINS", None)
    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/health",
                headers={"X-Forwarded-Proto": "http"},
                follow_redirects=False,
            )
            assert response.status_code in {301, 307, 308}
            assert response.headers.get("location", "").startswith("https://")
    finally:
        if prev_force is None:
            os.environ.pop("FORCE_HTTPS", None)
        else:
            os.environ["FORCE_HTTPS"] = prev_force
        if prev_origins is None:
            os.environ.pop("ALLOWED_ORIGINS", None)
        else:
            os.environ["ALLOWED_ORIGINS"] = prev_origins

