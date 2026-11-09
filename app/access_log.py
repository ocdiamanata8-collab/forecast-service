"""Log akses terstruktur — jejak permintaan untuk integrasi monitoring.

Satu baris per request HTTP, format key=value (bukan JSON) agar mudah
 di-parse pipeline log (ELK, Loki, dsb):

    ts=2026-10-01T13:04:11+08:00 level=info consumer=monitoring site=PLTS_KUPANG
    path=/api/v1/forecast/latest status=200 dur_ms=41 rid=8f2c

Yang sengaja TIDAK dicatat: isi body, header ``X-API-Key``, password dashboard.
``site_id`` diambil dari ``request.state`` (handler) atau query ``site_id``
 — body POST tidak dibaca di middleware.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from starlette.requests import Request

from app.preprocessing import DEFAULT_TIMEZONE

log = logging.getLogger("app.auth")

_SITE_Q_RE = re.compile(r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$")
_WITA = ZoneInfo(DEFAULT_TIMEZONE)


def configure_access_logging() -> None:
    """Logger ``app.auth`` hanya menulis baris key=value (tanpa prefix logging bawaan)."""
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.propagate = False


def short_request_id(request_id: str) -> str:
    """Empat karakter awal ID — cukup untuk korelasi singkat di log tail."""
    compact = (request_id or "").replace("-", "")
    return compact[:4] if compact else "-"


def site_for_request(request: Request) -> str:
    """Site terkait request tanpa membaca body."""
    stored = getattr(request.state, "site_id", None)
    if isinstance(stored, str) and stored:
        return stored
    q = request.query_params.get("site_id")
    if q and _SITE_Q_RE.fullmatch(q):
        return q
    return "-"


def format_wita_timestamp(when: datetime | None = None) -> str:
    """ISO-8601 dengan offset +08:00 (WITA)."""
    dt = (when or datetime.now(_WITA)).astimezone(_WITA)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        return f"{text[:-2]}:{text[-2:]}"
    return text


def format_access_line(
    *,
    path: str,
    status: int,
    dur_ms: int,
    request_id: str,
    site: str,
    consumer: str = "-",
    level: str = "info",
    rl_limit: str = "-",
    rl_remaining: str = "-",
    when: datetime | None = None,
) -> str:
    """Bangun satu baris log akses (tanpa newline)."""
    return (
        f"ts={format_wita_timestamp(when)} level={level} consumer={consumer} "
        f"site={site} path={path} status={status} dur_ms={dur_ms} "
        f"rid={short_request_id(request_id)} rl_limit={rl_limit} "
        f"rl_remaining={rl_remaining}"
    )


def log_http_access(
    request: Request,
    *,
    status: int,
    dur_ms: int,
    request_id: str,
    rl_limit: str = "-",
    rl_remaining: str = "-",
) -> None:
    """Catat satu request selesai — dipanggil dari middleware HTTP."""
    consumer = getattr(request.state, "api_key_label", "-") or "-"
    site = site_for_request(request)
    path = request.url.path
    level = "warning" if status >= 500 else "info"
    line = format_access_line(
        path=path,
        status=status,
        dur_ms=dur_ms,
        request_id=request_id,
        site=site,
        consumer=consumer,
        level=level,
        rl_limit=rl_limit,
        rl_remaining=rl_remaining,
    )
    log.info(line)
