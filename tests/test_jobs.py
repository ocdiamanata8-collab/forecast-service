"""Uji endpoint job internal — rantai §8 yang sama dengan scheduler & POST.

    POST /internal/v1/jobs/forecast

Prinsip yang diuji di sini (bukan sekadar "endpoint jalan"):

1. **Paritas** — dengan input history yang sama, hasil job (tersimpan di
   ``latest``) dan ``POST /forecast`` (on-demand) IDENTIK, kecuali dua
   penanda run yang memang harus berbeda: ``generated_at`` (kapan dibuat)
   dan ``forecast_id`` (idempotensi batch §8). Bila test ini gagal, klaim
   "endpoint dan job memanggil fungsi yang sama" sudah tidak berlaku —
   satu-satunya jalan gagal adalah ada yang menyalin langkah forecast
   alih-alih memanggil ``service.run_forecast``.
2. **Tulis sekali** — job berulang menambah batch (riwayat tidak ditimpa)
   dan batch lama tidak berubah isinya.
3. **Validasi & galat kontrak** — horizon ketat §6.1, site tak dikenal
   404 ``E_SITE_UNKNOWN``, galat domain → envelope §3.3, tanpa key 401.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SITE = "PLTS_KUPANG"
JOB = "/internal/v1/jobs/forecast"
#: Field yang MEMANG berbeda antar run — bukan bagian perbandingan paritas.
RUN_MARKERS = frozenset({"generated_at", "forecast_id"})


# ── akses & validasi ──────────────────────────────────────────────────────
def test_job_requires_api_key(client):
    """§1.1 — hanya /health yang terbuka; job MENULIS ke store."""
    r = client.post(JOB, json={"site_id": SITE, "horizon": 7})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E_UNAUTHORIZED"


@pytest.mark.parametrize("bad", [0, 31, -1, "7", 7.5, None])
def test_job_horizon_rejected_outside_contract(client, auth_headers, bad):
    """§6.1 + strict: di luar 1..30 ATAU salah tipe = 422 E_HORIZON_RANGE.

    Kode SAMA dengan POST /forecast (test_error_handling mengunci
    E_HORIZON_RANGE di sana) — endpoint job tidak boleh membalas kode
    generik, karena client monitoring mem-parse `code`, bukan `message`.
    """
    r = client.post(JOB, json={"site_id": SITE, "horizon": bad},
                    headers=auth_headers)
    assert r.status_code == 422, f"horizon={bad!r} harus ditolak"
    assert r.json()["error"]["code"] == "E_HORIZON_RANGE"


def test_job_default_horizon_is_24(client, auth_headers):
    """Spesifikasi job: default horizon 24 (bukan default scheduler 7)."""
    r = client.post(JOB, json={"site_id": SITE}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["horizon"] == 24
    assert r.json()["saved"] == 24


def test_job_unknown_site_is_404(client, auth_headers):
    """§6.1 — site salah konfigurasi ≠ forecast belum ada (E_SITE_UNKNOWN)."""
    r = client.post(JOB, json={"site_id": "PLTS_TIDAK_ADA", "horizon": 7},
                     headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_SITE_UNKNOWN"


# ── siklus simpan → baca ─────────────────────────────────────────────────
def test_job_saves_batch_readable_via_latest(client, auth_headers):
    """Kontrak §8: job menyimpan, lalu endpoint latest membaca batch yang sama."""
    r = client.post(JOB, json={"site_id": SITE, "horizon": 7}, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] == 7
    assert body["site_id"] == SITE
    uuid.UUID(body["forecast_id"])          # §3.2: UUID, bukan string bebas
    assert body["generated_at"].endswith("+08:00")   # §1.5: offset WITA

    r2 = client.get("/api/v1/forecast/latest",
                    params={"site_id": SITE}, headers=auth_headers)
    assert r2.status_code == 200
    stored = r2.json()
    assert stored["forecast_id"] == body["forecast_id"]
    assert len(stored["forecast"]) == 7
    assert stored["generated_at"] == body["generated_at"]


def test_job_twice_appends_batch_without_overwriting(client, auth_headers):
    """Tulis sekali (§8): run kedua = batch BARU; isi batch lama tidak berubah."""
    r1 = client.post(JOB, json={"site_id": SITE, "horizon": 7}, headers=auth_headers)
    r2 = client.post(JOB, json={"site_id": SITE, "horizon": 7}, headers=auth_headers)
    assert r1.status_code == r2.status_code == 200
    id1, id2 = r1.json()["forecast_id"], r2.json()["forecast_id"]
    assert id1 != id2, "setiap run harus batch baru (idempotensi per batch)"

    hist = client.get("/api/v1/forecast/history",
                      params={"site_id": SITE, "include_points": "true",
                              "limit": 10},
                      headers=auth_headers).json()["items"]
    assert len(hist) == 2, f"riwayat harus 2 batch, dapat {len(hist)}"
    by_id = {i["forecast_id"]: i for i in hist}
    # History tidak ditimpa: batch pertama masih utuh dengan nilai sama.
    assert set(by_id) == {id1, id2}
    pts1 = [p["forecast_value"] for p in by_id[id1]["forecast"]]
    pts2 = [p["forecast_value"] for p in by_id[id2]["forecast"]]
    assert pts1 == pts2, "history sama → nilai batch harus identik (determinisme)"


# ── PARITAS — inti klaim "fungsi yang sama" ───────────────────────────────
def test_job_and_post_on_demand_are_identical(client, auth_headers):
    """Input history identik → job (tersimpan) dan POST on-demand IDENTIK.

    Kecuali penanda run (``generated_at``, ``forecast_id``) yang memang
    lahir dari waktu pemanggilan. Field lain — termasuk ``ts``,
    ``forecast_time``, dan SELURUH ``forecast_value`` — wajib sama persis.
    """
    from app import scheduler

    # Sumber history yang sama dengan yang dipakai job: tail actuals.
    history = scheduler.fetch_history(SITE)
    assert len(history) >= 30, "butuh history cukup untuk POST (min 30)"

    on_demand = client.post(
        "/api/v1/forecast",
        json={
            "site_id": SITE, "target": "irradiance", "resolution": "daily",
            "horizon": 7,
            "history": [{"timestamp": ts.isoformat(), "value": v}
                        for ts, v in history],
        },
        headers=auth_headers,
    )
    assert on_demand.status_code == 200, on_demand.text

    job = client.post(JOB, json={"site_id": SITE, "horizon": 7},
                      headers=auth_headers)
    assert job.status_code == 200, job.text

    stored = client.get("/api/v1/forecast/latest",
                        params={"site_id": SITE}, headers=auth_headers).json()

    def strip_run_markers(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k not in RUN_MARKERS}

    assert strip_run_markers(on_demand.json()) == strip_run_markers(stored), (
        "hasil job (tersimpan) harus identik dengan POST on-demand bila "
        "history sama — selisih berarti rantai forecast sudah terpecah"
    )


def test_job_busy_lock_returns_409_not_duplicate_batch(client, auth_headers, monkeypatch):
    """Run paralel di site sama → 409 E_RUN_BUSY; tidak ada batch tersimpan."""
    from app.api import jobs as jobs_api
    from app.storage import ForecastRunBusy

    def busy(*_args, **_kwargs):
        raise ForecastRunBusy("run untuk site PLTS_KUPANG sudah berjalan di pemicu lain")

    # jobs.py mengimpor nama run_site_forecast langsung (bukan lewat modul),
    # jadi yang dipatch adalah atribut MODUL JOBS — pola sama dengan
    # test fetch_history di test_maps_domain_error (scheduler memanggil
    # lewat namespace modulnya).
    monkeypatch.setattr(jobs_api, "run_site_forecast", busy)
    r = client.post(JOB, json={"site_id": SITE, "horizon": 7}, headers=auth_headers)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "E_RUN_BUSY"
    assert set(err) >= {"code", "message", "request_id"}
    latest = client.get("/api/v1/forecast/latest", params={"site_id": SITE},
                        headers=auth_headers)
    assert latest.status_code == 404, "run yang dilewati tidak boleh menulis batch"


# ── galat domain → envelope §3.3 ─────────────────────────────────────────
def test_job_maps_domain_error_to_envelope(client, auth_headers, monkeypatch):
    """ForecastError dari rantai job → envelope kontrak (bukan 500/traceback)."""
    import app.scheduler as sched
    from app import service as svc

    def no_actuals(*args, **kwargs):
        raise svc.ForecastError("E_HISTORY_TOO_SHORT", "actuals kosong untuk site")

    monkeypatch.setattr(sched, "fetch_history", no_actuals)
    r = client.post(JOB, json={"site_id": SITE, "horizon": 7}, headers=auth_headers)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "E_HISTORY_TOO_SHORT"
    assert set(err) >= {"code", "message", "request_id"}  # §3.3
