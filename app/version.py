"""Sumber kebenaran versi TUNGGAL — file ``model/VERSION``.

Mengapa file ini terpisah dari ``app/engine.py``:

- ``app/engine.py`` dilarang melakukan I/O (uji kemurnian men-scan AST-nya).
  Membaca file di sana akan melanggar aturan itu.
- Versi adalah **data deploy**, bukan kode. Ganti model = edit satu baris,
  tanpa menyentuh kode dan tanpa risiko sisa hardcode yang terlewat.

Format ``model/VERSION``::

    v1.0                          <- baris pertama: model_version (wajib, semver)
    key=value                     <- metadata opsional, dipetakan oleh app/api/model.py

Dipakai oleh:

- ``model_version`` pada seluruh respons forecast (kontrak §3.1)
- ``service_version`` pada ``GET /api/v1/health`` (§5.6)
- ``model/VERSION`` juga dibaca ``app/api/model.py`` untuk metadata

Jangan pernah menulis literal versi di file lain — kalau menemukan
``"v1.x"`` hardcode di mana pun, itu bug.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

#: Path file ini. Satu-satunya tempat versi didefinisikan.
VERSION_FILE = Path(__file__).resolve().parents[1] / "model" / "VERSION"

#: Semver penuh (v1.0.0) atau dua digit (v1.0) — keduanya sah menurut semver.
#: Kontrak §3.1 sebelumnya memaksa tiga digit; dilonggarkan agar ``v1.0``
#: dari file yang sama diterima (perubahan non-breaking, kontrak §9.2).
SEMVER_PATTERN = re.compile(r"^v\d+\.\d+(\.\d+)?$")

#: Dipakai bila file tidak ada — jangan sampai terjadi pada produksi;
#: ``assert_sane()`` dipanggil lifespan untuk menagihnya.
FALLBACK_VERSION = "v0.0.0-unversioned"

__all__ = [
    "VERSION_FILE",
    "SEMVER_PATTERN",
    "FALLBACK_VERSION",
    "VersionFileError",
    "load_version",
    "model_version",
    "metadata",
    "field",
    "assert_sane",
]


class VersionFileError(RuntimeError):
    """File ``model/VERSION`` hilang atau isinya tidak sah."""


@lru_cache(maxsize=1)
def load_version() -> tuple[str, dict[str, str]]:
    """Baca ``model/VERSION`` sekali, lalu cache.

    Returns
    -------
    ``(model_version, metadata)``

    Raises
    ------
    VersionFileError
        Bila file hilang, baris pertama kosong, atau bukan semver.
    """
    if not VERSION_FILE.exists():
        raise VersionFileError(
            f"model/VERSION tidak ditemukan di {VERSION_FILE}. "
            "File ini sumber kebenaran versi — deploy tidak lengkap tanpanya."
        )

    raw = VERSION_FILE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]

    if not lines:
        raise VersionFileError(
            f"model/VERSION kosong ({VERSION_FILE}) — baris pertama harus model_version"
        )

    version = lines[0]
    if version.startswith("#"):
        raise VersionFileError(
            f"baris pertama model/VERSION berupa komentar; harus model_version"
        )
    if not SEMVER_PATTERN.match(version):
        raise VersionFileError(
            f"model_version '{version}' tidak valid — harus semver seperti v1.0 atau v1.0.0"
        )

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        metadata[key.strip()] = value.strip()

    return version, metadata


def _ensure_valid() -> tuple[str, dict[str, str]]:
    try:
        return load_version()
    except VersionFileError:
        return FALLBACK_VERSION, {}


def model_version() -> str:
    """``model_version`` untuk seluruh respons API."""
    return _ensure_valid()[0]


def metadata() -> dict[str, str]:
    """Metadata ``key=value`` dari file (tanpa baris versi)."""
    return dict(_ensure_valid()[1])


def field(name: str, default: str = "") -> str:
    """Ambil satu metadata; ``default`` bila tidak ada."""
    return _ensure_valid()[1].get(name, default)


def assert_sane() -> str:
    """Dipanggil saat startup supaya versi tak valid langsung terlihat, bukan
    pelan-pelan selama runtime.

    Mengembalikan ``FALLBACK_VERSION`` bila file bermasalah — lifespan
    mencatatnya ke respons ``/health`` sebagai ``degraded``.
    """
    version, _ = load_version()  # melempar bila file hilang/tidak sah
    return version
