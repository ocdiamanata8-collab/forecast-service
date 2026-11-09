"""Log akses terstruktur (app.access_log)."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from starlette.requests import Request

from app.access_log import (
    configure_access_logging,
    format_access_line,
    log_http_access,
    short_request_id,
    site_for_request,
)
from app.preprocessing import DEFAULT_TIMEZONE


def test_short_request_id():
    assert short_request_id("8f2c1a2b-64b4-bc59-69f9-1a5011c0bf8") == "8f2c"


def test_format_access_line_example():
    when = datetime(2026, 10, 1, 13, 4, 11, tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    line = format_access_line(
        path="/api/v1/forecast/latest",
        status=200,
        dur_ms=41,
        request_id="8f2c1a2b-0000-4000-8000-000000000001",
        site="PLTS_KUPANG",
        consumer="monitoring",
        when=when,
    )
    assert "ts=2026-10-01T13:04:11+08:00" in line
    assert "site=PLTS_KUPANG" in line
    assert "dur_ms=41" in line
    assert "rid=8f2c" in line


def test_site_from_query():
    scope = {"type": "http", "method": "GET", "path": "/x", "query_string": b"site_id=PLTS_KUPANG", "headers": []}
    assert site_for_request(Request(scope)) == "PLTS_KUPANG"


def test_log_no_api_key(caplog):
    from app.access_log import log as access_logger

    for handler in list(access_logger.handlers):
        access_logger.removeHandler(handler)
    access_logger.propagate = True
    caplog.set_level(logging.INFO, logger="app.auth")
    scope = {"type": "http", "method": "GET", "path": "/api/v1/health", "query_string": b"", "headers": []}
    req = Request(scope)
    req.state.api_key_label = "engineer"
    log_http_access(req, status=200, dur_ms=12, request_id="abcd1234-5678-90ab-cdef-1234567890ab")
    assert "super-secret" not in caplog.text
    assert "path=/api/v1/health" in caplog.records[0].message
    assert "consumer=engineer" in caplog.records[0].message
