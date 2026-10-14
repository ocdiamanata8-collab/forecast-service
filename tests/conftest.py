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

# Test-client default MENGUJI state kosong (store tanpa forecast, latest =
# 404 E_NO_FORECAST sesuai kontrak §5.2). Test scheduler mengaktifkan job
# secara eksplisit. Production tetap default aktif (SCHEDULER_ENABLED=true).
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Set API key di environment SEBELUM lifespan berjalan. Production tidak
# punya fallback key — tests harus mengaturnya sendiri.
DEMO_KEY = "dev-key-change-me-0123456789"
os.environ.setdefault("API_KEYS", DEMO_KEY)


@pytest.fixture(autouse=True)
def restore_api_keys_state():
    """Simpan & pulihkan API_KEYS + registry SEBELUM/SESUDAH setiap test.

    Test env (`test_env_keys.py`) secara sah mengubah API_KEYS dan
    mengosongkan registry untuk menguji perilaku parsing. Tanpa pemulihan
    otomatis, test-setelahnya menerima 401/403 dan kegagalannya
    menyesatkan — bukan schema yang rusak, melainkan kebocoran state.
    """
    import os as _os

    from app.security import get_registry

    saved_env = _os.environ.get("API_KEYS")
    saved_keys = dict(get_registry().keys)
    yield
    if saved_env is None:
        _os.environ.pop("API_KEYS", None)
    else:
        _os.environ["API_KEYS"] = saved_env
    registry = get_registry()
    registry.keys.clear()
    registry.keys.update(saved_keys)


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


@pytest.fixture(autouse=True)
def restore_scheduler_env():
    """Pulihkan SCHEDULER_ENABLED setiap test.

    Test scheduler mengaktifkan job secara eksplisit; tanpa pemulihan,
    test-setelahnya membuka lifespan dengan scheduler aktif — store terisi
    otomatis dan asersi 404 E_NO_FORECAST membingungkan.
    """
    saved = os.environ.get("SCHEDULER_ENABLED")
    yield
    if saved is None:
        os.environ["SCHEDULER_ENABLED"] = "false"
    else:
        os.environ["SCHEDULER_ENABLED"] = saved


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
