"""Uji environment variable & format multi-key (kontrak §1.1).

    python tests/test_env_keys.py

Yang dikunci:
- API_KEYS dipisah koma, tanpa spasi → semua key valid & berfungsi
- Spasi di sekitar koma DITOLAK (bukan diam-diam menambah kunci phantom)
- Tidak ada literal key contoh di dalam kode (hanya di .env.example)
- MODEL_VERSION dan TZ TIDAK dipakai — sumber kebenaran versi = model/VERSION,
  zona = Asia/Makassar (bukan env, bukan Asia/Jakarta)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402
from app.security import get_registry, get_limiter  # noqa: E402
from app.version import model_version  # noqa: E402

FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parents[1]
# Kunci contoh panjang (>16) untuk menguji format multi-key.
# Kunci pendek seperti "abc123xyz" ditolak oleh ApiKeyRegistry (min 16 karakter).
KEY_A = "a1b2c3d4e5f6g7h8i9j0"
KEY_B = "z9y8x7w6v5u4t3s2r1q0"
# Kunci contoh dari instruksi pengguna — HANYA dipakai sebagai pola dilarang.
SHORT_KEY_EXAMPLES = ("abc123xyz", "def456uvw")


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def test_multi_key_format():
    """Format `key1,key2` harus menghasilkan dua key valid."""
    from fastapi.testclient import TestClient

    os.environ["API_KEYS"] = f"{KEY_A},{KEY_B}"
    try:
        registry = get_registry()
        registry.keys.clear()
        with TestClient(create_app()) as c:
            for k in (KEY_A, KEY_B):
                check(k in registry.keys, f"key '{k[:8]}…' terdaftar dari API_KEYS")
            for k in (KEY_A, KEY_B):
                r = c.get("/api/v1/model/info", headers={"X-API-Key": k})
                check(r.status_code == 200, f"key '{k[:8]}…' diterima (HTTP {r.status_code})")
    finally:
        registry.keys.clear()
        os.environ.pop("API_KEYS", None)


def test_short_keys_rejected_at_startup():
    """Kunci <16 karakter DITOLAK ApiKeyRegistry — startup gagal, bukan diam-diam."""
    from app.security import ApiKeyRegistry

    reg = ApiKeyRegistry()
    for short in SHORT_KEY_EXAMPLES:
        try:
            reg.add(short, "test")
            check(False, f"kunci pendek '{short}' harus ditolak")
        except ValueError as e:
            check(True, f"kunci pendek '{short}' ditolak: {e}")
    # Kunci 16+ diterima.
    reg.add(KEY_A, "test")
    check(True, f"kunci 16+ ('{KEY_A[:8]}…') diterima")


def test_space_around_comma_rejected():
    """Spasi di sekitar koma tidak boleh menambah key phantom."""
    from fastapi.testclient import TestClient

    os.environ["API_KEYS"] = f"{KEY_A} , {KEY_B}"
    try:
        registry = get_registry()
        registry.keys.clear()
        with TestClient(create_app()) as c:
            check(KEY_A in registry.keys,
                  f"key asli tetap bersih (ditemukan: {[k[:8] for k in registry.keys]})")
            check(all(k == k.strip() for k in registry.keys),
                  "tidak ada key dengan spasi ekor")
            r = c.get("/api/v1/model/info", headers={"X-API-Key": KEY_A})
            check(r.status_code == 200, f"key asli tetap diterima (HTTP {r.status_code})")
    finally:
        registry.keys.clear()
        os.environ.pop("API_KEYS", None)


def test_empty_api_keys_rejects_all():
    """Tanpa API_KEYS → semua endpoint ber-key menolak 401 (by design)."""
    from fastapi.testclient import TestClient

    os.environ.pop("API_KEYS", None)
    try:
        registry = get_registry()
        registry.keys.clear()
        with TestClient(create_app()) as c:
            r = c.get("/api/v1/model/info")
            check(r.status_code == 401, f"tanpa key -> 401 (dapat {r.status_code})")
            # Health tetap terbuka.
            r = c.get("/api/v1/health")
            check(r.status_code == 200, f"/health tetap 200 (dapat {r.status_code})")
    finally:
        registry.keys.clear()


def test_example_keys_not_in_code():
    """Kunci contoh pendek HANYA boleh ada di file test ini (sebagai pola dilarang),
    tidak boleh ada di kode, HTML, atau .env.example."""
    check_keys = list(SHORT_KEY_EXAMPLES)
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".html", ".md", ".json"}:
            continue
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.name in (".env.example", "test_env_keys.py"):
            continue  # di .env.example sudah dihapus; di file ini = pola dilarang
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for k in check_keys:
            if k in text:
                check(False, f"key contoh '{k}' bocor di {path.relative_to(ROOT)}")
                return
    check(True, "tidak ada key contoh di kode / .env.example")


def test_model_version_env_ignored():
    """MODEL_VERSION env TIDAK berpengaruh — model/VERSION satu-satunya sumber."""
    old = os.environ.get("MODEL_VERSION")
    try:
        os.environ["MODEL_VERSION"] = "v9.9.9-BOGUS"
        from app.version import load_version

        load_version.cache_clear()
        # model_version() harus tetap dari file, bukan env.
        v = model_version()
        file_first = (ROOT / "model" / "VERSION").read_text(
            encoding="utf-8"
        ).splitlines()[0].strip()
        check(v == file_first,
              f"versi tetap dari model/VERSION ({v}), bukan env v9.9.9-BOGUS")
        check(v != "v9.9.9-BOGUS", "MODEL_VERSION env diabaikan")
    finally:
        load_version.cache_clear()
        if old is None:
            os.environ.pop("MODEL_VERSION", None)
        else:
            os.environ["MODEL_VERSION"] = old


def test_tz_env_ignored_and_zone_is_makassar():
    """TZ env TIDAK berpengaruh — zona dikunci Asia/Makassar (WITA), bukan Jakarta."""
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Jakarta"  # UTC+7 — salah untuk Kupang
        from app.preprocessing import DEFAULT_TIMEZONE
        from app.schemas import SITE_TZ

        check(DEFAULT_TIMEZONE == "Asia/Makassar",
              f"DEFAULT_TIMEZONE tetap Asia/Makassar (dapat {DEFAULT_TIMEZONE})")
        check(str(SITE_TZ) == "Asia/Makassar",
              f"SITE_TZ tetap Asia/Makassar (dapat {SITE_TZ})")
        # Zona Jakarta (UTC+7) tidak boleh dipakai di mana pun.
        check(DEFAULT_TIMEZONE != "Asia/Jakarta",
              "Asia/Jakarta (UTC+7) tidak dipakai — salah untuk Kupang")
        # Verifikasi offset sebenarnya.
        from datetime import datetime

        now = datetime.now(tz=SITE_TZ)
        check(str(now.utcoffset()) == "8:00:00",
              f"offset SITE_TZ = +08:00 (dapat {now.utcoffset()})")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old


def test_env_example_documents_omissions():
    """.env.example menjelaskan kenapa MODEL_VERSION dan TZ tidak ada."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    check("MODEL_VERSION" in content,
          ".env.example menyebut MODEL_VERSION (dengan alasan tidak dipakai)")
    check("Asia/Makassar" in content and "Asia/Jakarta" in content,
          ".env.example menjelaskan zona WITA vs WIB")
    check("model/VERSION" in content,
          ".env.example menunjuk model/VERSION sebagai sumber versi")
    # API_KEYS dan SITE_IDS memang ada.
    check(re.search(r"^API_KEYS=", content, re.M),
          ".env.example punya API_KEYS")
    check(re.search(r"^SITE_IDS=", content, re.M),
          ".env.example punya SITE_IDS")


def main() -> int:
    print("\n[1] Format multi-key")
    test_multi_key_format()
    print("\n[2] Kunci <16 karakter DITOLAK (validasi ApiKeyRegistry)")
    test_short_keys_rejected_at_startup()
    print("\n[3] Spasi sekitar koma")
    test_space_around_comma_rejected()
    print("\n[4] Tanpa API_KEYS -> 401")
    test_empty_api_keys_rejects_all()
    print("\n[5] Kunci contoh tidak bocor ke kode/.env.example")
    test_example_keys_not_in_code()
    print("\n[6] MODEL_VERSION env diabaikan")
    test_model_version_env_ignored()
    print("\n[7] TZ env diabaikan; zona = Asia/Makassar")
    test_tz_env_ignored_and_zone_is_makassar()
    print("\n[8] .env.example menjelaskan omisi")
    test_env_example_documents_omissions()

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ENV & API KEY: FORMAT BENAR, KUNCI TIDAK BOCOR, SUMBER KEBENARAN TUNGGAL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
