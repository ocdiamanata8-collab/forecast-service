"""Konfigurasi pytest untuk forecast-service.

Menambah root repo ke ``sys.path`` supaya ``app.*`` bisa diimpor dari
lokasi mana pun tanpa ``pip install -e``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set API key di environment SEBELUM lifespan berjalan. Production tidak
# punya fallback key — tests harus mengaturnya sendiri.
DEMO_KEY = "dev-key-change-me-0123456789"
os.environ.setdefault("API_KEYS", DEMO_KEY)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Bersihkan penghitung rate limit SEBELUM setiap test.

    ``_LIMITER`` adalah state modul yang bertahan antar test. Tanpa reset,
    test ke-61 dalam suite besar menerima 429 dan menutupi asersi lain —
    kegagalan yang terlihat seolah-olah berasal dari schema, padahal dari
    antrean request sebelumnya. Isolasi ini juga membuat test rate limit
    sendiri deterministik.
    """
    from app.security import get_limiter

    limiter = get_limiter()
    limiter._hits.clear()
    yield
    limiter._hits.clear()


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": DEMO_KEY}


@pytest.fixture()
def client():
    """HTTP client uji — tanpa ``TestClient`` agar dependensi tetap minimal."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
