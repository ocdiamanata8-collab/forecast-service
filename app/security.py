"""Keamanan (API key) dan penyusun envelope error sesuai kontrak §3.3.

Mengapa ada di satu berkas: keduanya menyentuh **semua** endpoint dengan
cara yang sama, dan keduanya adalah keputusan kontrak (kode error), bukan
logika domain. Mengganti backend autentikasi nanti cukup mengganti fungsi
``require_api_key`` tanpa menyentuh handler.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

#: Skema security OpenAPI — inilah yang membuat tombol "Authorize" muncul di
#: /docs. Tanpa ini, engineer TIDAK BISA menguji endpoint yang butuh key
#: dari Swagger: key tidak bisa dikirim, semua percobaan berakhir 401.
#: Dideklarasikan sekali, dipakai ulang oleh semua endpoint (kontrak §1.1).
API_KEY_NAME = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_NAME,
    scheme_name="ApiKeyAuth",
    description=(
        "API key untuk seluruh endpoint kecuali GET /api/v1/health. "
        "Dikirim sebagai header X-API-Key; di /docs isi lewat tombol Authorize."
    ),
    auto_error=False,  # error-nya ditangani require_api_key (envelope §3.3)
)

# ---------------------------------------------------------------------------
# Envelope error (kontrak §3.3)
# ---------------------------------------------------------------------------
HTTP_CODE_LABELS = {
    "E_UNAUTHORIZED": 401,
    "E_FORBIDDEN": 403,
    "E_VALIDATION_FAILED": 422,
    "E_HORIZON_RANGE": 422,
    "E_TZ_REQUIRED": 422,
    "E_HISTORY_TOO_SHORT": 422,
    "E_IRREGULAR_SERIES": 422,
    "E_DUPLICATE_TIMESTAMP": 422,
    "E_VALUE_INVALID": 422,
    "E_TARGET_UNAVAILABLE": 422,
    "E_EXOG_INCOMPLETE": 422,
    "E_EXOG_MISSING_COLUMN": 422,
    "E_TIME_RANGE_INVALID": 422,
    "E_LIMIT_RANGE": 422,
    "E_CURSOR_INVALID": 400,
    "E_CURSOR_FILTER_MISMATCH": 422,
    "E_IDEMPOTENCY_MISMATCH": 409,
    "E_SITE_UNKNOWN": 404,
    "E_NO_FORECAST": 404,
    "E_RATE_LIMITED": 429,
    "E_INTERNAL": 500,
    "E_SERVICE_UNAVAILABLE": 503,
    "E_MODEL_FAILED": 500,
    "E_NOT_FOUND": 404,
    "E_METHOD_NOT_ALLOWED": 405,
}


def request_id_of(request: Request) -> str:
    """ID pelacakan respons — sama dengan header ``X-Request-Id``."""
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = str(uuid.uuid4())
        request.state.request_id = rid
    return rid


def error_response(
    request: Request,
    code: str,
    message: str,
    *,
    status: int | None = None,
    details: list[dict[str, object]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Bangun ``ErrorEnvelope`` (§3.3) dengan status dan ``X-Request-Id``."""
    status_code = status or HTTP_CODE_LABELS.get(code, 500)
    rid = request_id_of(request)
    payload: dict[str, object] = {
        "error": {"code": code, "message": message, "request_id": rid}
    }
    if details:
        payload["error"]["details"] = details  # type: ignore[index]
    all_headers = {"X-Request-Id": rid}
    if headers:
        all_headers.update(headers)
    return JSONResponse(status_code=status_code, content=payload, headers=all_headers)


# ---------------------------------------------------------------------------
# Autentikasi
# ---------------------------------------------------------------------------
@dataclass
class ApiKeyRegistry:
    """Registri API key sementara untuk PoC.

    Pada Fase berikutnya, ganti penyimpannya (env var / DB) tanpa mengubah
    pemanggil di endpoint. ``durable`` menandai apakah key ini dianggap
    permanen atau hanya contoh — kunci contoh diberi label agar tidak
    disalahartikan sebagai produksi.
    """

    keys: dict[str, str] = field(default_factory=dict)  # key -> label
    durable: bool = False

    def add(self, key: str, label: str, *, durable: bool = True) -> None:
        if len(key) < 16:
            raise ValueError("API key minimal 16 karakter")
        self.keys[key] = label
        self.durable = self.durable or durable

    @staticmethod
    def generate() -> str:
        return secrets.token_urlsafe(32)


@dataclass
class RateLimiter:
    """Rate limit kontrak §1.3 — 60 request/menit per API key.

    Implementasi ringan: jendela bergerak per key. Tidak memakai RNG —
    penghitungannya deterministik terhadap pola panggilan.
    """

    limit: int = 60
    window_seconds: int = 60
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """Kembalikan ``(allowed, remaining, retry_after_seconds)``."""
        current = time.monotonic() if now is None else now
        window_start = current - self.window_seconds
        hits = [t for t in self._hits.get(key, []) if t > window_start]
        if len(hits) >= self.limit:
            retry = int(max(1.0, self.window_seconds - (current - hits[0])))
            self._hits[key] = hits
            return False, 0, retry
        hits.append(current)
        self._hits[key] = hits
        return True, self.limit - len(hits), 0


_REGISTRY = ApiKeyRegistry()
_LIMITER = RateLimiter()


def get_registry() -> ApiKeyRegistry:
    """Akses registri (dipakai ``main`` untuk mengisi key dari konfigurasi)."""
    return _REGISTRY


def get_limiter() -> RateLimiter:
    return _LIMITER


def require_api_key(
    request: Request,
    x_api_key: str | None = Depends(api_key_header),
) -> str:
    """Dependency FastAPI: validasi ``X-API-Key`` (kontrak §1.1).

    Mendaftarkan ``X-API-Key`` sebagai security scheme OpenAPI (tombol
    Authorize di /docs), lalu:

    - Header hilang -> ``401 E_UNAUTHORIZED``
    - Key tak dikenal / dicabut -> ``403 E_FORBIDDEN``
    - Lewat rate limit -> ``429 E_RATE_LIMITED`` dengan ``Retry-After``

    Raise :class:`FastApiError` agar handler middleware yang membentuk respons.
    """
    if not x_api_key:
        raise FastApiError("E_UNAUTHORIZED", "missing X-API-Key header")
    registry = get_registry()
    if x_api_key not in registry.keys:
        raise FastApiError("E_FORBIDDEN", "invalid or revoked API key")

    allowed, remaining, retry_after = get_limiter().check(x_api_key)
    if not allowed:
        raise FastApiError(
            "E_RATE_LIMITED",
            f"rate limit {get_limiter().limit} req/min exceeded",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(get_limiter().limit),
                "X-RateLimit-Remaining": "0",
            },
        )
    return x_api_key


def require_model_ready(request: Request) -> None:
    """Gerbang kesiapan model (kontrak §6.1 → ``503 E_SERVICE_UNAVAILABLE``).

    Diterapkan HANYA pada endpoint yang butuh model hidup:

    - ``POST /api/v1/forecast`` — memanggil engine.
    - ``GET  /api/v1/model/info`` — melaporkan metadata model yang harus
      benar-benar termuat; melaporkan metadata model yang tidak siap
      menyesatkan konsumen.

    Sengaja TIDAK dipasang di ``require_api_key``: ``latest``/``history``
    membaca penyimpanan dan tetap sah disajikan walau model sedang
    dimuat ulang. Dengan begitu monitoring bisa tetap membaca forecast
    lama saat proses deploy berjalan, tapi tidak pernah menerima forecast
    segar dari model yang belum siap.

    Tanpa guard ini, ``model_loaded=False`` diam-diam tetap merespons 200
    dengan angka hasil model yang seharusnya tidak dipercaya.
    """
    if not bool(getattr(request.app.state, "model_loaded", False)):
        raise FastApiError(
            "E_SERVICE_UNAVAILABLE",
            "model is not ready yet; retry after the service finishes loading "
            "(see GET /api/v1/health)",
            status=503,
            headers={"Retry-After": "10"},
        )


def assert_known_site(request: Request, site_id: str) -> None:
    """Kontrak §6.1 → ``404 E_SITE_UNKNOWN`` bila site tidak terdaftar.

    Membedakan dua kondisi yang sebelumnya tampak sama di monitoring:

    - ``E_SITE_UNKNOWN``   — site_id salah / belum terdaftar (konfigurasi
      salah: nama site salah eja, atau site baru belum didaftarkan via
      env ``SITE_IDS``).
    - ``E_NO_FORECAST``    — site valid, forecast belum pernah dibuat
      (wajar di startup; scheduler belum jalan).

    Tanpa pembagian ini, platform monitoring tidak bisa membedakan
    "salah konfigurasi" dari "belum ada data".
    """
    known = getattr(request.app.state, "known_sites", None)
    if known is not None and site_id not in known:
        raise FastApiError(
            "E_SITE_UNKNOWN",
            f"site_id '{site_id}' is not registered; "
            "check SITE_IDS configuration or register the site first",
            status=404,
        )


class FastApiError(Exception):
    """Galat kontrak yang dibawa dari dependency ke middleware/envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: list[dict[str, object]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        self.headers = headers or {}
