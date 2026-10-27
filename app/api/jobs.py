"""Endpoint job internal: pemicu siklus forecast terjadwal per permintaan.

    POST /internal/v1/jobs/forecast    {"site_id": ..., "horizon": 24}

Menerjemahkan pola "job" menjadi rantai yang **sama persis** dengan
scheduler Fase 4 (kontrak §8) — bukan menyalin langkahnya::

    scheduler.run_site_forecast(store, site_id, horizon=...)
      = fetch_history (tail actuals) → service.run_forecast → store.save

Karena endpoint ini, scheduler terjadwal, dan ``POST /api/v1/forecast``
semuanya berujung ke fungsi ``service.run_forecast`` yang sama, perilaku
keduanya dijamin identik: validasi artefak, urutan fitur, scaling,
klip/pembulatan, satuan, dan aturan zona. Yang membedakan hanya

=====================  ==================  =========================
asal history          pemanggilan         menyimpan?
=====================  ==================  =========================
``POST /forecast``     body request        tidak (on-demand, §5.1)
job ini                tail ``actuals.csv`` YA — tulis sekali, §8
scheduler terjadwal    tail ``actuals.csv`` YA — tulis sekali, §8
=====================  ==================  =========================

Keputusan yang menyimpang dari sketsa awal (dengan alasan):

* **API key wajib.** Sketsa tanpa auth; repo mengunci "hanya ``/health``
  yang terbuka" dan endpoint ini MENULIS ke store — tanpa key, pihak luar
  bisa memicu beban model & menulis batch. Pakai ``require_api_key``
  + ``require_model_ready`` seperti ``POST /forecast``.
* **Horizon divalidasi kontrak §6.1** (strict int 1..30) — sama ketatnya
  dengan ``ForecastRequest``, bukan ``int`` polos yang lolos 7.5 dan 0.
* **Galat domain dipetakan ke envelope §3.3** (adapter yang sama dengan
  ``api.forecast.run_forecast``): ``ForecastError`` → ``FastApiError``.

Lapisan: file ini hanya penerjemah HTTP → ``scheduler.run_site_forecast``
(tidak ada langkah forecast yang ditulis ulang — lihat README "Pemisahan
lapisan"; menyalin langkahnya di sini justru yang mengancam jaminan
identik).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app import service
from app.scheduler import DEFAULT_HORIZON, run_site_forecast
from app.schemas import StrictSchema
from app.security import (
    FastApiError,
    assert_known_site,
    require_api_key,
    require_model_ready,
)
from app.storage import ForecastRunBusy

router = APIRouter(prefix="/internal/v1", tags=["jobs"])

#: Batas horizon §6.1 untuk resolusi daily (1..30) — identik dengan
#: ``ForecastRequest.horizon``. Default 24 mengikuti spesifikasi job.
HORIZON_PATTERN = r"^\d+$"


class ForecastJobRequest(StrictSchema):
    """Body job — validasi sama ketatnya dengan kontrak §3.

    ``horizon`` strict: ``"7"`` (string) dan ``7.5`` harus DITOLAK, bukan
    dinomalkan diam-diam (regresi yang sama dengan ``ForecastRequest``).
    Default satu sumber dengan warm tick/cron: ``DEFAULT_HORIZON``.
    """

    site_id: str = Field(pattern=r"^[A-Z]{2,4}[_A-Z0-9]{2,12}$")
    horizon: int = Field(default=DEFAULT_HORIZON, ge=1, le=30, strict=True)


class ForecastJobResponse(StrictSchema):
    """Hasil satu run job. ``generated_at``/``forecast_id`` = penanda run —
    dua job berbeda menghasilkan batch berbeda (tulis sekali, §8)."""

    saved: int
    site_id: str
    horizon: int
    forecast_id: str
    generated_at: datetime


@router.post(
    "/jobs/forecast",
    response_model=ForecastJobResponse,
    # Endpoint INTERNAL — sengaja tidak muncul di OpenAPI. Guard repo
    # (test_contract_fields, test_dashboard) mengunci "Swagger hanya
    # menampilkan endpoint kontrak"; endpoint ini bukan kontrak publik
    # (pemanggil = cron/CI internal), jadi memaparkannya di /docs hanya
    # menjauhkan konsumen dari 7 endpoint yang memang untuk mereka.
    include_in_schema=False,
)
def forecast_job(
    payload: ForecastJobRequest,
    request: Request,
    _: str = Depends(require_api_key),
    __: None = Depends(require_model_ready),
) -> ForecastJobResponse:
    """Jalankan siklus forecast untuk ``site_id`` lalu simpan hasilnya.

    Rantai pemanggilan **identik** dengan scheduler (kontrak §8): bila job
    ini dan tick scheduler dipanggil pada waktu yang sama, keduanya
    menulis batch dengan isi yang sama. Respons hanya melaporkan apa yang
    tersimpan — tidak ada nilai forecast yang dihitung ulang di sini.
    """
    # Site salah konfigurasi → 404 E_SITE_UNKNOWN, sama seperti endpoint
    # lain: lebih baik gagal di sini daripada menulis batch yang tak pernah
    # bisa dicocokkan platform monitoring.
    assert_known_site(request, payload.site_id)
    store = request.app.state.store
    try:
        item = run_site_forecast(store, payload.site_id, horizon=payload.horizon)
    except ForecastRunBusy as exc:
        # Pemicu lain (mis. Actions + cron yang overlap) sedang memegang
        # kunci untuk site ini. Skip, JANGAN antre: antrean tetap menulis
        # batch duplikat sesudah run pertama selesai.
        raise FastApiError("E_RUN_BUSY", str(exc), status=409) from exc
    except service.ForecastError as exc:
        # Adapter error yang sama dengan api.forecast.run_forecast:
        # galat domain (mis. E_HISTORY_TOO_SHORT) → envelope §3.3.
        raise FastApiError(exc.code, exc.message, status=exc.status) from exc
    return ForecastJobResponse(
        saved=len(item.forecast),
        site_id=item.site_id,
        horizon=item.horizon,
        forecast_id=item.forecast_id,
        generated_at=item.generated_at,
    )
