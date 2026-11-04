"""Autentikasi dashboard engineer (HTTP Basic) — lapisan sederhana untuk PoC.

Dashboard engineer memuat data internal (forecast, aktual, evaluasi model)
dan punya tombol menjalankan job. Tanpa lapisan ini, siapa pun yang
membuka URL-nya bisa melihat data internal dan memicu engine.

PoC ini memakai **HTTP Basic** sampai platform SSO tersedia:

- Kredensial dari env ``DASHBOARD_USER`` / ``DASHBOARD_PASSWORD``.
- Tanpa konfigurasi → dashboard **ditolak** (fail-closed), bukan terbuka.
- Perbandingan password memakai ``secrets.compare_digest`` (timing-safe).
- Percobaan login dibatasi (rate limit per IP+user) supaya tidak bisa
  ditebak tanpa batas.
- Basic Auth hanya aman di atas HTTPS —hence ``FORCE_HTTPS=true``.

CATATAN PENTING: ini lapisan **halaman**, terpisah dari API key
(``X-API-Key``, kontrak §1.1). Halaman tetap menaruh API key di
``localStorage`` browser dan mengirimkannya sebagai header — kedua lapisan
berdampingan, bukan saling menggantikan.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.security import FastApiError

log = logging.getLogger("app.dashboard_auth")

#: Skema OpenAPI — tombol "Authorize" di /docs.
dashboard_basic = HTTPBasic(
    scheme_name="DashboardBasic",
    realm="Engineer Console",
    description=(
        "Dashboard engineer (GET /). Basic Auth sederhana untuk PoC; "
        "akan diganti SSO platform."
    ),
    auto_error=False,
)

#: Jumlah percobaan login gagal yang boleh terjadi per (IP, user) dalam
#: jendela ``LOGIN_WINDOW_SECONDS``.
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300


@dataclass
class LoginLimiter:
    """Pembatas percobaan login gagal — berbasis monotonic time."""

    max_attempts: int = LOGIN_MAX_ATTEMPTS
    window_seconds: int = LOGIN_WINDOW_SECONDS
    _fails: dict[str, list[float]] = field(default_factory=dict)

    def is_blocked(self, bucket: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        window_start = current - self.window_seconds
        hits = [t for t in self._fails.get(bucket, []) if t > window_start]
        self._fails[bucket] = hits
        return len(hits) >= self.max_attempts

    def record_failure(self, bucket: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self._fails.setdefault(bucket, []).append(current)

    def reset(self, bucket: str) -> None:
        self._fails.pop(bucket, None)


_LOGIN_LIMITER = LoginLimiter()


def get_login_limiter() -> LoginLimiter:
    return _LOGIN_LIMITER


def dashboard_credentials() -> tuple[str, str]:
    """Baca ``DASHBOARD_USER``/``DASHBOARD_PASSWORD`` dari env.

    Kembalikan ``("", "")`` bila belum dikonfigurasi — pemanggil harus
    menolak akses (fail-closed), bukan menerima semua.
    """
    user = (os.environ.get("DASHBOARD_USER") or "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD") or ""
    return user, password


def dashboard_auth_configured() -> bool:
    user, password = dashboard_credentials()
    return bool(user) and bool(password)


def log_dashboard_auth_at_startup() -> None:
    """Catat status konfigurasi dashboard auth saat startup (tanpa nilai)."""
    user, password = dashboard_credentials()
    if not user or not password:
        log.warning(
            "dashboard engineer: DASHBOARD_USER/DASHBOARD_PASSWORD belum "
            "di-set — GET / akan DITOLAK (401). Isi keduanya di secret "
            "manager sebelum deploy."
        )
        return
    if len(password) < 12:
        log.warning(
            "dashboard engineer: DASHBOARD_PASSWORD untuk user '%s' kurang "
            "dari 12 karakter — disarankan panjang minimal 16",
            user,
        )
    log.info("dashboard engineer: HTTP Basic aktif (user=%s)", user)


def _client_ip(request: Request) -> str:
    """IP klien — dari header proxy bila ada, else address langsung.

    Catatan: spoofing ``X-Forwarded-For`` hanya melewati pembatas percobaan
    login, bukan melewati verifikasi kredensial.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_dashboard_login(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(dashboard_basic),
) -> str:
    """Verifikasi kredensial dashboard engineer.

    - Tanpa kredensial → ``401`` + header ``WWW-Authenticate``.
    - Kredensial salah → ``401`` (identik, agar tak ada oracle "user ada").
    - Belum dikonfigurasi → ``503`` (deploy belum siap), bukan 401.
    """
    user, password = dashboard_credentials()
    if not user or not password:
        raise FastApiError(
            "E_SERVICE_UNAVAILABLE",
            "dashboard engineer is not configured: set DASHBOARD_USER and "
            "DASHBOARD_PASSWORD (secret manager) before exposing GET /",
            status=503,
        )

    bucket = f"{_client_ip(request)}|{(credentials.username if credentials else '-')}"
    if get_login_limiter().is_blocked(bucket):
        raise FastApiError(
            "E_RATE_LIMITED",
            "too many failed dashboard login attempts; try again later",
            status=429,
            headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)},
        )

    ok = (
        credentials is not None
        and credentials.username is not None
        and secrets.compare_digest(credentials.username, user)
        and secrets.compare_digest(credentials.password or "", password)
    )
    if not ok:
        get_login_limiter().record_failure(bucket)
        log.warning(
            "dashboard login GAGAL user=%s ip=%s",
            credentials.username if credentials else "-",
            _client_ip(request),
        )
        raise FastApiError(
            "E_UNAUTHORIZED",
            "invalid dashboard credentials",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Engineer Console"'},
        )

    get_login_limiter().reset(bucket)
    request.state.dashboard_user = user
    return user
