"""FastAPI app + mount routes.

Jalankan lokal:

    uvicorn app.main:app --reload --port 8000

Ketentuan lapisan ini:

- **Hanya** menyusun router, middleware envelope error, dan lifespan.
  Tidak ada logika forecast di sini.
- Semua endpoint di bawah ``/api/v1`` (kontrak §1: versi di URL).
- Error dinormalisasi ke ``ErrorEnvelope`` (§3.3) + header ``X-Request-Id``.
- ``GET /api/v1/health`` dibuka tanpa key; endpoint lain lewat ``require_api_key``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import forecast as forecast_api
from app.api import health as health_api
from app.api import model as model_api
from app.artifacts import ArtifactError, load_artifacts
from app.scheduler import enabled_from_env, interval_from_env, scheduler_loop, tick as scheduler_tick
from app.security import (
    FastApiError,
    error_response,
    get_registry,
    parse_api_keys,
    request_id_of,
)
from app.storage import InMemoryForecastStore
from app.version import VersionFileError, assert_sane

log = logging.getLogger("app.main")
usage_log = logging.getLogger("app.auth")


#: Pesan manusiawi per kode (kontrak §3.3). ``message`` boleh bebas berubah
#: seiring waktu — client hanya boleh memparse ``code`` — tetapi setiap
#: respons tetap harus membawa kalimat yang masuk akal untuk ditampilkan
#: di UI monitoring, bukan pesan generik "request validation failed".
VALIDATION_MESSAGE_BY_CODE: dict[str, str] = {
    "E_VALIDATION_FAILED": "request body does not match the API contract; "
                           "see details[] for the exact fields",
    "E_HORIZON_RANGE": "horizon is out of bounds: must be 1-30 for daily resolution",
    "E_HISTORY_TOO_SHORT": "data history is empty or too short: minimum 30 "
                           "daily observations required",
    "E_TZ_REQUIRED": "timestamp is missing an explicit UTC offset; "
                     "timezone must not be guessed",
    "E_IRREGULAR_SERIES": "history timestamps are not evenly spaced; "
                          "supply one reading per day",
    "E_DUPLICATE_TIMESTAMP": "history contains duplicate timestamps",
    "E_EXOG_MISSING_COLUMN": "exog payload is incomplete: it must contain every "
                             "column listed in columns",
    "E_EXOG_INCOMPLETE": "exog does not cover the full forecast window",
}


def _coerce_detail_value(raw: object) -> str | int | float | bool | None:
    """Kembalikan value details dalam tipe aslinya bila memungkinkan.

    Kontrak §3.3 memberi contoh ``{"value": 45}`` — angka, bukan string.
    Mengubah semua jadi string membuat client harus menebak-nebak tipe.
    Namun payload besar (list/dict) dipangkas jadi ringkas supaya envelope
    tidak meledak ukurannya.
    """
    if isinstance(raw, bool | int | float):
        if isinstance(raw, float) and not math.isfinite(raw):
            return str(raw)
        return raw
    if raw is None:
        return None
    text = str(raw)
    # Angka yang tertulis sebagai string tetap dikembalikan sebagai angka.
    try:
        return int(text)
    except ValueError:
        pass
    try:
        parsed = float(text)
        if math.isfinite(parsed):
            return parsed
    except ValueError:
        pass
    return text[:120]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Siapkan penyimpanan + API key sekali saat proses hidup."""
    app.state.started_at = time.monotonic()
    app.state.store = InMemoryForecastStore()
    app.state.performance_cache = {}

    # Root handler SEBELUM log pertama: tanpa ini log INFO app (usage log,
    # scheduler, registrasi key) tenggelam di default WARNING uvicorn.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ------------------------------------------------------------------
    # API key per konsumen (§1.1) — format env: `API_KEYS=label:key,...`
    #
    # Satu konsumen (platform monitoring, CI, engineer) satu key, dengan
    # LABEL di env: pemakaian dipantau per konsumen lewat log (bukan lewat
    # nilai key — nilai key tidak pernah dicatat), dan pencabutan per
    # konsumen = menghapus satu entri dari env lalu deploy ulang. Key hanya
    # dari environment — TIDAK ada fallback key di kode; belum dikonfigurasi
    # = semua endpoint ber-key menolak 401. Format salah label/key <16 char
    # = gagal startup (jelas sejak awal, bukan diam-diam membatalkan konsumen).
    # ------------------------------------------------------------------
    registry = get_registry()
    parsed_keys = parse_api_keys(os.environ.get("API_KEYS", ""))
    for key, label in parsed_keys.items():
        if key not in registry.keys:
            registry.add(key, label, durable=True)
    # Visibilitas startup untuk operator: konsumen apa saja yang aktif.
    # HANYA label & jumlah — nilai key tidak pernah dicatat.
    log.info(
        "api key aktif: %d entri, konsumen=%s",
        len(registry.keys),
        ",".join(sorted(set(registry.keys.values()))) or "-",
    )

    # Daftar site yang dikenal layanan (kontrak §6.1 → 404 E_SITE_UNKNOWN
    # untuk latest/history/performance). Tanpa registry ini, site_id yang
    # salah ejaan tidak bisa dibedakan dari site valid yang belum berforecast.
    app.state.known_sites = set(
        filter(None, (s.strip() for s in os.environ.get("SITE_IDS", "PLTS_KUPANG").split(",")))
    )

    # Versi dari model/VERSION — diperiksa sekali di startup supaya file
    # hilang/rusak langsung terlihat, bukan diam-diam selama runtime.
    try:
        assert_sane()
        app.state.version_ok = True
    except VersionFileError:
        app.state.version_ok = False

    # Artefak model juga divalidasi saat startup: urutan kolom, target, dan
    # skala harus terbaca sebelum melayani request (kontrak: API tidak boleh
    # menebak urutan fitur). Gagal di sini = jelas sejak awal, bukan 500 di
    # tengah request produksi.
    try:
        load_artifacts()
        app.state.artifacts_ok = True
    except ArtifactError:
        app.state.artifacts_ok = False

    # model dianggap termuat bila artefak/manifest ada (lihat app/api/model.py).
    app.state.model_loaded = True

    # ------------------------------------------------------------------
    # Scheduler rolling forecast (Fase 4 — kontrak §5.1/§8).
    #
    # POST /forecast TIDAK menyimpan hasil; pengisi latest/history adalah
    # job latar belakang ini, memanggil rantai IDENTIK (service.run_forecast).
    # Tick pertama dijalankan dan DITUNGGU saat startup supaya
    # GET /forecast/latest langsung 200 sebelum deploy dilaporkan selesai —
    # monitoring tidak perlu menunggu satu jam. Interval kemudian per jam
    # (§1.4 / SLA §5.2). Matikan dengan SCHEDULER_ENABLED=false (test &
    # proses worker sekunder).
    # ------------------------------------------------------------------
    app.state.scheduler_enabled = enabled_from_env()
    app.state.scheduler_interval = interval_from_env()
    app.state.scheduler_task = None
    if app.state.scheduler_enabled:
        sites = sorted(app.state.known_sites)
        await asyncio.to_thread(
            scheduler_tick, app.state.store, sites
        )
        app.state.scheduler_task = asyncio.create_task(
            scheduler_loop(
                app.state.store, sites, interval=app.state.scheduler_interval,
                immediate=False,  # tick pertama sudah dijalankan & ditunggu di atas
            )
        )
        log.info(
            "scheduler aktif: site=%s interval=%ss",
            sites, app.state.scheduler_interval,
        )
    yield

    task = app.state.scheduler_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


#: Folder template dashboard — level modul supaya bisa dipatch saat test.
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def create_app() -> FastAPI:
    from app.version import model_version

    app = FastAPI(
        title="Forecasting Service PLTS Kupang",
        version=model_version(),
        description=(
            "Forecast irradiance harian untuk PLTS Kupang. "
            "Kontrak resmi: KONTRAK_API_v1.md — dokumentasi di /docs adalah "
            "cerminan kontrak, bukan sumber kebenaran."
        ),
        lifespan=lifespan,
    )

    app.include_router(forecast_api.router)
    app.include_router(model_api.router)
    app.include_router(health_api.router)

    # Adapter sisi konsumen monitoring (format internal <- kontrak API).
    # File murni JS, tanpa route/OpenAPI baru untuk endpoint data.
    static_dir = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---------------------------------------------------------------------------
    # GET / — halaman dashboard engineer.
    #
    # Halaman ini adalah KONSUMEN API, sama seperti platform monitoring: ia
    # hanya menyajikan shell HTML lalu JS-nya memanggil /api/v1/* melalui
    # fetch. Tidak ada logika forecast, tidak ada data yang dibakar ke
    # response — sehingga perubahan kontrak API langsung ketahuan saat halaman
    # dibuka (audit §3 di JS melaporkan FAIL).
    #
    # Tanpa API key di sini: key disimpan di localStorage browser dan dikirim
    # oleh JS pada setiap request API.
    # ---------------------------------------------------------------------------
    # CATATAN: HTMLResponse harus diimport di tingkat MODUL (baris atas file).
    # Dengan `from __future__ import annotations`, anotasi `-> HTMLResponse`
    # disimpan sebagai string dan dipakai pydantic sebagai forward reference
    # yang dicari di namespace modul. Pydantic 2.9 (terkunci di requirements)
    # menolak bila nama itu hanya tersedia lokal di dalam fungsi — crash saat
    # startup container. Pydantic 2.13 di laptop kebetulan toleran, jadi masalah
    # ini baru terlihat saat build image. Inilah gunanya menguji container.
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def engineer_dashboard() -> HTMLResponse:
        index = TEMPLATE_DIR / "index.html"
        if not index.exists():
            raise FastApiError(
                "E_NOT_FOUND",
                f"dashboard template missing: {index} "
                "(deploy rusak — pastikan templates/ ikut ter-copy)",
                status=404,
            )
        return HTMLResponse(index.read_text(encoding="utf-8"))

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        # ------------------------------------------------------------------
        # Log pemakaian per konsumen (§1.1) — SATU baris per request ber-key.
        # Format key=value siap diparse platform monitoring/log pipeline.
        # label konsumen dari API_KEYS=label:key; nilai KEY TIDAK PERNAH ikut
        # dicatat (kebocoran key ke log = pelanggaran kebersihan rahasia).
        # health/docs tanpa key tercatat consumer=- (probe uptime bukan konsumen).
        # ------------------------------------------------------------------
        consumer = getattr(request.state, "api_key_label", "-")
        usage_log.info(
            "usage consumer=%s method=%s path=%s status=%s request_id=%s "
            "rl_limit=%s rl_remaining=%s",
            consumer,
            request.method,
            request.url.path,
            response.status_code,
            rid,
            response.headers.get("X-RateLimit-Limit", "-"),
            response.headers.get("X-RateLimit-Remaining", "-"),
        )
        return response

    @app.exception_handler(FastApiError)
    async def fastapi_error_handler(request: Request, exc: FastApiError):
        return error_response(
            request, exc.code, exc.message, status=exc.status,
            details=exc.details, headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        details = []
        errors = exc.errors()
        for err in errors:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            details.append({
                "field": loc or "body",
                "issue": err.get("type", "invalid"),
                "value": _coerce_detail_value(err.get("input")),
            })

        # Kode error spesifik (kontrak §6.1) — client MEMPARSE `code`, bukan
        # `message`. Mengembalikan E_VALIDATION_FAILED untuk semua kondisi
        # membuat konsumen tidak bisa membedakan horizon salah rentang dari
        # kolom hilang.
        code = "E_VALIDATION_FAILED"
        for err in errors:
            etype = err.get("type", "")
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = str(err.get("msg", ""))

            if loc.endswith("horizon") and etype in {"greater_than", "less_than",
                                                     "greater_than_equal",
                                                     "less_than_equal",
                                                     "int_parsing", "int_type"}:
                code = "E_HORIZON_RANGE"
                break
            if "explicit UTC offset" in msg or (
                etype == "value_error" and "offset" in msg
            ):
                code = "E_TZ_REQUIRED"
                break
            if loc.endswith("history") and etype in {"too_short", "missing"}:
                code = "E_HISTORY_TOO_SHORT"
                break
            if "regular intervals" in msg:
                code = "E_IRREGULAR_SERIES"
                break
            if "ascending" in msg or "unique" in msg:
                code = "E_DUPLICATE_TIMESTAMP" if "unique" in msg else "E_IRREGULAR_SERIES"
                break

        return error_response(
            request,
            code,
            VALIDATION_MESSAGE_BY_CODE.get(code, "request validation failed"),
            status=422,
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = {
            404: "E_NOT_FOUND",
            405: "E_METHOD_NOT_ALLOWED",
            503: "E_SERVICE_UNAVAILABLE",
        }.get(exc.status_code, "E_INTERNAL")
        headers = dict(getattr(exc, "headers", None) or {})
        if exc.status_code == 405 and "Allow" not in headers:
            headers["Allow"] = "GET, POST"
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return error_response(request, code, detail, status=exc.status_code, headers=headers)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # Jangan membocorkan stack trace ke client (kontrak §6).
        return error_response(
            request, "E_INTERNAL", "internal server error", status=500
        )

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Pintu masuk pengembangan (bukan kontrak): python -m app.main
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD", "")),
    )
