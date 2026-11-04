"""Keamanan (API key) dan penyusun envelope error sesuai kontrak §3.3.

Mengapa ada di satu berkas: keduanya menyentuh **semua** endpoint dengan
cara yang sama, dan keduanya adalah keputusan kontrak (kode error), bukan
logika domain. Mengganti backend autentikasi nanti cukup mengganti fungsi
``require_api_key`` tanpa menyentuh handler.
"""

from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials

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
    """Registri API key: satu entri per KONSUMEN (§1.1).

    ``keys`` memetakan ``key -> label konsumen`` (mis. ``monitoring``, ``ci``)
    sehingga pemakaian bisa dipantau dan dicabut per konsumen — label hanya
    metadata untuk log/operasi; verifikasi request tetap lookup key saja
    (kontrak §1.1 tidak mengenal scope/permission).

    Pada Fase berikutnya, ganti penyimpannya (env var / DB) tanpa mengubah
    pemanggil di endpoint. ``durable`` menandai apakah key ini dianggap
    permanen atau hanya contoh — kunci contoh diberi label agar tidak
    disalahartikan sebagai produksi.
    """

    keys: dict[str, str] = field(default_factory=dict)  # key -> label konsumen
    #: key -> tanggal kedaluwarsa (``None`` = permanen, format §1.1 opsional).
    #: Dievaluasi per request — key mati pada tanggalnya TANPA restart.
    expires: dict[str, date] = field(default_factory=dict)
    durable: bool = False

    def add(
        self,
        key: str,
        label: str,
        *,
        durable: bool = True,
        expires: date | None = None,
    ) -> None:
        if len(key) < 16:
            raise ValueError("API key minimal 16 karakter")
        self.keys[key] = label
        self.expires[key] = expires
        self.durable = self.durable or durable

    @staticmethod
    def generate() -> str:
        return secrets.token_urlsafe(32)


#: Label konsumen: huruf kecil di awal, lalu huruf/angka/dash/underscore.
#: Sengaja ketat — label ikut masuk log dan dipakai operasi revoke, jadi
#: spasi/`=`/`:` akan membuat log sulit diparse.
CONSUMER_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

#: Label untuk entri `API_KEYS` tanpa prefix label (format lama).
LEGACY_LABEL = "legacy"

#: Tanggal kedaluwarsa key: TEPAT `YYYY-MM-DD` (regex dulu — ``date.fromisoformat``
#: menerima juga format padat ``YYYYMMDD`` di Python ≥3.11; format padat
#: ditolak supaya satu-satunya bentuk yang sah adalah yang didokumentasikan.
KEY_EXPIRY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iter_api_key_entries(raw: str):
    """Yield ``(label, key, expiry)`` per entri ``API_KEYS``.

    Ekstensi format §1.1 (kompatibel mundur — suffix opsional)::

        label:key@YYYY-MM-DD     # key berkedaluwarsa
        key@YYYY-MM-DD           # polos + expiry -> label ``legacy``
        label:key                # lama: permanen (tetap sah)

    ``@`` aman sebagai pemisah expiry: alphabet ``secrets.token_urlsafe``
    tidak memuat ``@`` dan label dilarang memuatnya (pola konsumen), jadi
    partition pertama tidak pernah memotong bagian sah dari key.

    Expiry bertujuan menutup celah paling umum pada service sejenis:
    **key yang tidak pernah dicabut setelah satu orang pindah tugas**.
    Lewat tanggal (tanggal server) key mati sendiri di request berikutnya —
    tanpa menunggu restart. Tanggal salah format → ``ValueError`` saat
    startup (gagal jelas, bukan key yang diam-diam permanen).

    Raise ``ValueError`` pada label/key/expiry tidak sah (lihat
    :func:`parse_api_keys`).
    """
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        body, at_sign, expiry_raw = entry.partition("@")
        expiry = None
        if at_sign:
            if not KEY_EXPIRY_PATTERN.match(expiry_raw):
                raise ValueError(
                    f"kedaluwarsa key tidak sah: {expiry_raw!r} — format "
                    "wajib YYYY-MM-DD (mis. 'monitoring:KUNCI@2027-04-01')"
                )
            expiry = date.fromisoformat(expiry_raw)
        if ":" in body:
            label, key = body.split(":", 1)
            label = label.strip()
            key = key.strip()
            if not CONSUMER_LABEL_PATTERN.match(label):
                raise ValueError(
                    f"label konsumen tidak sah: {label!r} — harus pola "
                    f"{CONSUMER_LABEL_PATTERN.pattern} (mis. 'monitoring:KUNCI')"
                )
        else:
            label, key = LEGACY_LABEL, body
        if len(key) < 16:
            raise ValueError(
                f"API key '{label}' minimal 16 karakter (dapat {len(key)})"
            )
        yield label, key, expiry


def parse_api_keys_full(raw: str) -> dict[str, tuple[str, date | None]]:
    """Parse ``API_KEYS`` → ``{key: (label, expiry)}`` — dipakai startup.

    Raise ``ValueError`` pada entri tidak sah — dipanggil saat startup
    sehingga konfigurasi salah GAGAL SEJAK AWAL, bukan diam-diam membatalkan
    konsumen (pola sama dengan penolakan key <16 karakter).
    """
    return {key: (label, expiry) for label, key, expiry in _iter_api_key_entries(raw)}


def parse_api_keys(raw: str) -> dict[str, str]:
    """Parse env ``API_KEYS`` → ``{key: label}`` per konsumen.

    Wrapper kompatibel atas :func:`parse_api_keys_full` (expiry tidak
    memengaruhi label). Lihat format di ``_iter_api_key_entries``.
    """
    return {key: label for label, key, _expiry in _iter_api_key_entries(raw)}


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

    def reset(self, key: str) -> None:
        """Hapus hit counter untuk satu key (mis. setelah login berhasil)."""
        self._hits.pop(key, None)


_REGISTRY = ApiKeyRegistry()
_LIMITER = RateLimiter()
#: Kuota terpisah untuk POST on-demand (§5.1) — jauh lebih ketat dari §1.3
#: agar sistem lain tidak bisa mem-polling engine ARIMA; operasional pakai
#: GET /forecast/latest (§5.2).
_ON_DEMAND_LIMITER = RateLimiter(limit=6, window_seconds=60)


def get_registry() -> ApiKeyRegistry:
    """Akses registri (dipakai ``main`` untuk mengisi key dari konfigurasi)."""
    return _REGISTRY


def get_limiter() -> RateLimiter:
    return _LIMITER


def get_on_demand_limiter() -> RateLimiter:
    return _ON_DEMAND_LIMITER


def configure_on_demand_limiter() -> None:
    """Terapkan env ``ON_DEMAND_FORECAST_RATE_*`` sekali saat startup."""
    import os

    lim = get_on_demand_limiter()
    lim.limit = max(1, int(os.environ.get("ON_DEMAND_FORECAST_RATE_LIMIT", "6")))
    lim.window_seconds = max(
        1, int(os.environ.get("ON_DEMAND_FORECAST_RATE_WINDOW", "60"))
    )


def on_demand_blocked_consumer_labels() -> frozenset[str]:
    """Label konsumen yang dilarang POST /forecast (default: ``monitoring``).

    Env ``FORECAST_ON_DEMAND_BLOCKED_LABELS`` — daftar dipisah koma.
    Kosong / ``none`` / ``-`` → tidak ada yang diblokir (hanya untuk uji).
    """
    import os

    raw = os.environ.get("FORECAST_ON_DEMAND_BLOCKED_LABELS", "monitoring").strip()
    if not raw or raw.lower() in {"none", "-"}:
        return frozenset()
    return frozenset(filter(None, (p.strip() for p in raw.split(","))))


def require_api_key(
    request: Request,
    response: Response,
    x_api_key: str | None = Depends(api_key_header),
) -> str:
    """Dependency FastAPI: validasi ``X-API-Key`` (kontrak §1.1).

    Mendaftarkan ``X-API-Key`` sebagai security scheme OpenAPI (tombol
    Authorize di /docs), lalu:

    - Header hilang -> ``401 E_UNAUTHORIZED``
    - Key tak dikenal / dicabut -> ``403 E_FORBIDDEN``
    - Lewat rate limit -> ``429 E_RATE_LIMITED`` dengan ``Retry-After``

    Identitas konsumen (label dari ``API_KEYS=label:key``) disimpan di
    ``request.state.api_key_label`` — dipakai middleware untuk LOG PEMAKAIAN
    per konsumen (bukan untuk otorisasi; kontrak §1.1 biner). Header
    ``X-RateLimit-Limit``/``-Remaining`` dilekatkan ke respons SUKSES
    (§1.3 — sebelumnya hanya di 429).

    Raise :class:`FastApiError` agar handler middleware yang membentuk respons.
    """
    if not x_api_key:
        raise FastApiError("E_UNAUTHORIZED", "missing X-API-Key header")
    registry = get_registry()
    label = registry.keys.get(x_api_key)
    if label is None:
        raise FastApiError("E_FORBIDDEN", "invalid or revoked API key")

    # Kedaluwarsa (§1.1) — SEBELUM rate limit: key mati tidak boleh menguras
    # kuota, dan keputusan diambil per request (lewat tanggal = mati sendiri,
    # tanpa menunggu restart — menutup celah "tidak pernah dicabut").
    expiry = registry.expires.get(x_api_key)
    if expiry is not None and date.today() > expiry:
        raise FastApiError(
            "E_FORBIDDEN",
            f"API key expired on {expiry.isoformat()} — rotate the key "
            "per kontrak §1.1 (label sama, key baru)",
            status=403,
        )

    # Disetel SEBELUM cek rate limit: baris 429 pun harus tercatat sebagai
    # pemakaian konsumen ini (middleware membaca label ini).
    request.state.api_key_label = label

    allowed, remaining, retry_after = get_limiter().check(x_api_key)
    if not allowed:
        raise FastApiError(
            "E_RATE_LIMITED",
            f"rate limit {get_limiter().limit} req/min exceeded",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(get_limiter().limit),
                "X-RateLimit-Remaining": "0",
                # §1.3 menjanjikan header ini sejak draft pertama tapi belum
                # pernah dikirim — detik sampai jendela kuota ter-reset.
                "X-RateLimit-Reset": str(retry_after),
            },
        )
    # §1.3: header rate limit juga di respons yang lolos (bukan hanya 429).
    response.headers["X-RateLimit-Limit"] = str(get_limiter().limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return x_api_key


def require_on_demand_forecast(
    request: Request,
    response: Response,
    x_api_key: str | None = Depends(api_key_header),
) -> str:
    """Gerbang khusus ``POST /api/v1/forecast`` (§5.1).

    Jalur operasional monitoring memakai ``GET /forecast/latest`` (§5.2).
    Endpoint ini tetap ada untuk uji engineer, dengan:

    - penolakan eksplisit konsumen berlabel (default ``monitoring``) → ``403``;
    - kuota terpisah ``ON_DEMAND_FORECAST_RATE_*`` (bawaan 6/menit), lebih
      ketat dari §1.3 agar client lain tidak mem-polling engine ARIMA.

    Autentikasi + rate limit umum §1.3 tetap lewat :func:`require_api_key`.
    Header ``X-RateLimit-*`` pada respons sukses POST mencerminkan kuota
    on-demand, bukan 60 req/menit endpoint lain.
    """
    key = require_api_key(request, response, x_api_key)
    label = request.state.api_key_label

    if label in on_demand_blocked_consumer_labels():
        raise FastApiError(
            "E_FORBIDDEN",
            "POST /api/v1/forecast is for engineer testing only; "
            "use GET /api/v1/forecast/latest for operational polling",
            status=403,
        )

    od = get_on_demand_limiter()
    allowed, remaining, retry_after = od.check(x_api_key)
    if not allowed:
        raise FastApiError(
            "E_RATE_LIMITED",
            f"on-demand forecast rate limit {od.limit} req/"
            f"{od.window_seconds}s exceeded; use GET /api/v1/forecast/latest "
            "instead of polling POST /forecast",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(od.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(retry_after),
            },
        )
    response.headers["X-RateLimit-Limit"] = str(od.limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return key


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
