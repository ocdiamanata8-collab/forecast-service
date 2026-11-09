"""Kebijakan HTTP: CORS terbatas dan paksa HTTPS (deployment).

Dashboard monitoring dihosting terpisah membutuhkan CORS eksplisit;
``allow_origins=[\"*\"]`` dengan header ``X-API-Key`` dari browser
membuka celah: halaman asing bisa memakai key pengunjung. Origin
hanya dari env ``ALLOWED_ORIGINS`` (koma, tanpa wildcard).

HTTPS: ``FORCE_HTTPS=true`` mengaktifkan redirect HTTP→HTTPS plus
``ProxyHeadersMiddleware`` agar ``X-Forwarded-Proto`` dari reverse
proxy (Render, Blitz, nginx) dipercaya.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

log = logging.getLogger("app.http_policy")

# Origin browser: scheme + host (+ port opsional), tanpa path/query.
_ORIGIN_RE = re.compile(
    r"^https?://"
    r"(?![\s/])"
    r"[^\s/?#]+"
    r"(?::\d{1,5})?$"
)


def _truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_allowed_origins(raw: str) -> list[str]:
    """Parse ``ALLOWED_ORIGINS`` — koma, tanpa wildcard.

    Spasi di sekitar koma ditolak (konsisten dengan ``API_KEYS``).
    """
    text = (raw or "").strip()
    if not text:
        return []
    if "," in text and re.search(r",\s|\s,", text):
        raise ValueError(
            "ALLOWED_ORIGINS tidak boleh berisi spasi di sekitar koma"
        )
    origins: list[str] = []
    for part in text.split(","):
        origin = part.strip()
        if not origin:
            raise ValueError("ALLOWED_ORIGINS mengandung entri kosong")
        if origin == "*":
            raise ValueError(
                "ALLOWED_ORIGINS=* dilarang — sebut origin penuh "
                "(mis. https://monitoring.sei.co.id)"
            )
        if not _ORIGIN_RE.fullmatch(origin):
            raise ValueError(
                f"ALLOWED_ORIGINS entri tidak valid: {origin!r} "
                "(harus https://host atau http://host[:port])"
            )
        parsed = urlparse(origin)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError(
                f"ALLOWED_ORIGINS tidak boleh memuat path/query: {origin!r}"
            )
        normalized = origin.rstrip("/")
        if normalized not in origins:
            origins.append(normalized)
    return origins


def allowed_origins_from_env() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    return parse_allowed_origins(raw)


def force_https_enabled() -> bool:
    return _truthy(os.environ.get("FORCE_HTTPS"))


def trusted_proxy_hosts_from_env() -> str:
    return (os.environ.get("TRUSTED_PROXY_HOSTS", "*") or "*").strip() or "*"


def configure_http_middleware(app: FastAPI) -> None:
    """Pasang CORS (bila origin dikonfigurasi) dan redirect HTTPS bila diminta."""
    origins = allowed_origins_from_env()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "X-API-Key",
                "Content-Type",
                "Authorization",
                "X-Request-Id",
                "Idempotency-Key",
            ],
            expose_headers=[
                "X-Request-Id",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
        )

    if force_https_enabled():
        trusted = trusted_proxy_hosts_from_env()
        # Urutan WAJIB: ProxyHeaders di LUAR HTTPSRedirect.
        #
        # Starlette ``add_middleware`` menyisipkan di posisi paling luar
        # (index 0 = middleware paling depan), jadi middleware yang
        # ditambahkan TERAKHIR menjadi yang TERLUAR. ProxyHeaders harus
        # paling luar karena ia yang mengubah ``scope["scheme"]`` dari
        # ``X-Forwarded-Proto`` milik reverse proxy (Render, Blitz, nginx).
        #
        # Jika urutannya terbalik (redirect di luar proxy-headers),
        # request dari proxy TLS selalu terlihat ``http`` — karena header
        # forwarded baru dibaca SETELAH middleware redirect selesai —
        # sehingga setiap request dialihkan 307 ke URL https yang sama.
        # Browser/curl mengikuti redirect itu dan mengulang selamanya:
        # ERR_TOO_MANY_REDIRECTS / "redirected you too many times".
        app.add_middleware(HTTPSRedirectMiddleware)
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)


def log_http_policy_at_startup() -> None:
    """Catat kebijakan CORS/HTTPS sekali saat lifespan."""
    try:
        origins = allowed_origins_from_env()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if origins:
        log.info(
            "cors: %d origin diizinkan (%s)",
            len(origins),
            ",".join(origins),
        )
    else:
        log.info(
            "cors: ALLOWED_ORIGINS kosong — permintaan cross-origin browser "
            "diblokir; dashboard same-origin (/static) tidak terpengaruh"
        )
    if force_https_enabled():
        log.info(
            "https: FORCE_HTTPS aktif — redirect HTTP→HTTPS, "
            "proxy trusted_hosts=%s",
            trusted_proxy_hosts_from_env(),
        )
    else:
        log.info(
            "https: FORCE_HTTPS tidak aktif (set FORCE_HTTPS=true di produksi)"
        )
