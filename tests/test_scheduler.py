"""Uji scheduler rolling forecast (Fase 4) — pengisi latest/history latar belakang.

    pytest tests/test_scheduler.py -q

Yang dikunci:

1. History diambil dari tail `data/actuals.csv` (bukan jendela "hari ini"
   yang bisa kosong bila dataset lebih tua dari jam sekarang).
2. `run_and_store_forecast` = rantai identik endpoint: `service.run_forecast`
   → `store.save` → `GET /forecast/latest` 200 (bukan 404 E_NO_FORECAST).
3. Determinisme: input & generated_at sama → forecast[] identik.
4. `SCHEDULER_ENABLED=false` → lifespan tidak menjalankan job; latest tetap
   404 E_NO_FORECAST (kondisi kontrak §5.2 yang sah, bukan bug).
5. Kegagalan SATU site di `tick()` dicatat, tidak mematikan site berikutnya;
   loop berjalan terus tiap interval.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Console Windows default cp1252 — samakan UTF-8 (pola sama dengan suite lain).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Mode script tidak memuat conftest — set state dasar dulu.
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import scheduler, service  # noqa: E402
from app.main import create_app  # noqa: E402
from app.preprocessing import DEFAULT_TIMEZONE  # noqa: E402
from app.storage import ForecastRunBusy, InMemoryForecastStore  # noqa: E402

TZ = ZoneInfo(DEFAULT_TIMEZONE)
KEY = "dev-key-change-me-0123456789"
H = {"X-API-Key": KEY}


def history(n: int = 90) -> list[tuple[datetime, float]]:
    """Deret harian deterministik tz-aware (pola test_service_layer)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz=DEFAULT_TIMEZONE)
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    return [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]


# ---------------------------------------------------------------------------
# 1. Sumber history = tail actuals
# ---------------------------------------------------------------------------
def test_fetch_history_uses_latest_actuals_tail():
    got = scheduler.fetch_history("PLTS_KUPANG", rows=60)
    assert len(got) == 60
    # Tail dataset (2025-12-31), bukan jendela mundur dari "hari ini".
    assert got[-1][0].date().isoformat() == "2025-12-31"
    assert got[0][0].date().isoformat() == "2025-11-02"
    assert all(ts.tzinfo is not None for ts, _ in got)


def test_fetch_history_rejects_empty_window(monkeypatch):
    """Site tanpa data aktual → ForecastError, bukan crash loop."""
    def empty(*, site_id, start, end):
        return pd.DataFrame(
            {"actual_irradiance": pd.Series(dtype="float64")},
            index=pd.DatetimeIndex([], tz=TZ, name="timestamp"),
        )

    monkeypatch.setattr(scheduler, "load_actual_series", empty)
    with pytest.raises(service.ForecastError) as exc:
        scheduler.fetch_history("PLTS_KUPANG", rows=60)
    assert exc.value.code == "E_HISTORY_TOO_SHORT"


# ---------------------------------------------------------------------------
# 2. Simpan + endpoint latest
# ---------------------------------------------------------------------------
def test_run_and_store_persists_forecast():
    store = InMemoryForecastStore()
    assert store.latest("PLTS_KUPANG") is None

    item = scheduler.run_and_store_forecast(
        store,
        site_id="PLTS_KUPANG",
        history=history(),
        horizon=7,
        generated_at=datetime(2026, 10, 14, 9, 0, tzinfo=TZ),
    )

    latest = store.latest("PLTS_KUPANG")
    assert latest is not None
    assert latest.forecast_id == item.forecast_id
    assert latest.horizon == 7
    assert len(latest.forecast) == 7


def test_latest_endpoint_returns_scheduler_result(monkeypatch):
    store = InMemoryForecastStore()
    saved = scheduler.run_and_store_forecast(
        store, site_id="PLTS_KUPANG", history=history(), horizon=7
    )

    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    with TestClient(create_app()) as client:
        client.app.state.store = store
        r = client.get("/api/v1/forecast/latest",
                       params={"site_id": "PLTS_KUPANG"}, headers=H)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["forecast_id"] == saved.forecast_id
    assert len(body["forecast"]) == 7
    assert body["order"] == "ARIMA(1, 0, 2)"  # konsisten §5.2 (WITA order)
    assert body["generated_at"].endswith("+08:00")


# ---------------------------------------------------------------------------
# 3. Determinisme
# ---------------------------------------------------------------------------
def test_scheduler_deterministic_forecast_values():
    gen = datetime(2026, 10, 14, 9, 0, tzinfo=TZ)
    a = scheduler.run_and_store_forecast(
        InMemoryForecastStore(), site_id="PLTS_KUPANG", history=history(),
        horizon=7, generated_at=gen,
    )
    b = scheduler.run_and_store_forecast(
        InMemoryForecastStore(), site_id="PLTS_KUPANG", history=history(),
        horizon=7, generated_at=gen,
    )
    assert a.forecast_id != b.forecast_id  # UUID baru per tick
    assert a.forecast == b.forecast         # angka identik
    assert a.generated_at == b.generated_at


# ---------------------------------------------------------------------------
# 4. Sakelar env
# ---------------------------------------------------------------------------
def test_scheduler_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    with TestClient(create_app()) as client:
        assert client.app.state.scheduler_enabled is False
        assert client.app.state.scheduler_task is None
        # Tidak ada satu pun task asyncio dari lifespan (loop sudah dihapus;
        # guard ini yang mencegah loop kembali lewat refactor berikutnya).
        assert client.app.state.backfill_task is None
        assert client.app.state.retention_task is None
        assert client.app.state.store.count() == 0
        r = client.get("/api/v1/forecast/latest",
                       params={"site_id": "PLTS_KUPANG"}, headers=H)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "E_NO_FORECAST"


def test_scheduler_enabled_runs_startup_warm_tick_only(monkeypatch):
    """Startup performs one warm tick; web process creates no recurring task."""
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    with TestClient(create_app()) as client:
        assert client.app.state.scheduler_enabled is True
        assert client.app.state.scheduler_task is None
        assert client.app.state.backfill_task is None
        assert client.app.state.retention_task is None
        r = client.get("/api/v1/forecast/latest",
                       params={"site_id": "PLTS_KUPANG"}, headers=H)
        assert r.status_code == 200, r.text
        assert len(r.json()["forecast"]) == scheduler.DEFAULT_HORIZON == 24


# ---------------------------------------------------------------------------
# 5. Kegagalan per-site tidak mematikan tick; site berikutnya tetap diproses
# ---------------------------------------------------------------------------
def test_tick_survives_failed_site(monkeypatch):
    store = InMemoryForecastStore()

    def boom(_store, site_id, **_kw):
        raise service.ForecastError("E_HISTORY_TOO_SHORT", "actuals hilang")

    monkeypatch.setattr(scheduler, "run_site_forecast", boom)
    saved = scheduler.tick(store, ["PLTS_GAGAL", "PLTS_JUGA_GAGAL"])
    assert saved == []
    assert store.count() == 0  # tidak ada yang tersimpan, tapi tidak melempar


def test_tick_continues_to_next_site_after_failure(monkeypatch):
    store = InMemoryForecastStore()
    attempted: list[str] = []

    def fake(_store, site_id, **_kw):
        attempted.append(site_id)
        if site_id == "PLTS_GAGAL":
            raise RuntimeError("sintetis")
        return scheduler.run_and_store_forecast(
            _store, site_id=site_id, history=history(), horizon=7
        )

    monkeypatch.setattr(scheduler, "run_site_forecast", fake)
    saved = scheduler.tick(store, ["PLTS_GAGAL", "PLTS_KUPANG"])

    assert attempted == ["PLTS_GAGAL", "PLTS_KUPANG"]  # tick tidak berhenti
    assert len(saved) == 1
    assert store.latest("PLTS_KUPANG") is not None


# ---------------------------------------------------------------------------
# 6. Anti-tumpang-tindih: kunci advisory per site (duck-typed di store)
# ---------------------------------------------------------------------------
class _LockRecorderStore(InMemoryForecastStore):
    """Store in-memory + site_run_lock yang mencatat urutan enter/exit."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []

    @contextmanager
    def site_run_lock(self, site_id: str):
        self.events.append(("lock", site_id))
        try:
            yield
        finally:
            self.events.append(("unlock", site_id))


class _BusyLockStore(InMemoryForecastStore):
    """Store yang selalu menganggap site sedang diproses pemicu lain."""

    @contextmanager
    def site_run_lock(self, site_id: str):
        raise ForecastRunBusy(f"run untuk site {site_id} sudah berjalan")
        yield  # pragma: no cover — contextmanager wajib yield


def test_run_is_wrapped_by_site_lock_when_store_supports_it(monkeypatch):
    """Kunci dikeluarkan SEBELUM fetch dan dilepas SESUDAH save+record."""
    store = _LockRecorderStore()
    hist = history()
    fetched: list[str] = []

    def fake_fetch(site_id, **_kw):
        fetched.append(site_id)
        assert store.events == [("lock", "PLTS_KUPANG")], \
            "fetch harus terjadi DALAM kunci, bukan sebelumnya"
        return hist

    monkeypatch.setattr(scheduler, "fetch_history", fake_fetch)
    scheduler.run_site_forecast(store, "PLTS_KUPANG")

    assert fetched == ["PLTS_KUPANG"]
    assert store.events == [("lock", "PLTS_KUPANG"), ("unlock", "PLTS_KUPANG")]
    assert store.latest("PLTS_KUPANG") is not None  # run tetap menyimpan


def test_busy_lock_skips_run_before_fetch_or_save(monkeypatch):
    """ForecastRunBusy dilempar SEBELUM fetch — run kedua tak menyentuh data."""
    store = _BusyLockStore()

    def no_fetch(*_a, **_kw):  # pragma: no cover — tak boleh tercapai
        raise AssertionError("fetch tidak boleh dijalankan saat lock sibuk")

    monkeypatch.setattr(scheduler, "fetch_history", no_fetch)
    with pytest.raises(ForecastRunBusy, match="sudah berjalan"):
        scheduler.run_site_forecast(store, "PLTS_KUPANG")
    assert store.latest("PLTS_KUPANG") is None, "run sibuk tidak boleh menyimpan"


def test_tick_skips_busy_site_without_treating_it_as_failure(monkeypatch):
    """Site sibuk DILEWATI (bukan error, bukan run failed) — site lain lanjut."""
    store = _BusyLockStore()

    @contextmanager
    def busy_only_for_busy_site(site_id: str):
        if site_id == "ZZ_BUSY":
            raise ForecastRunBusy("lagi diproses pemicu lain")
        yield

    store.site_run_lock = busy_only_for_busy_site
    monkeypatch.setattr(scheduler, "fetch_history", lambda _s, **_kw: history())

    saved = scheduler.tick(store, ["ZZ_BUSY", "PLTS_KUPANG"])
    # Site sibuk tidak masuk daftar tersimpan DAN tidak menghentikan loop.
    assert len(saved) == 1
    assert store.latest("PLTS_KUPANG") is not None
    assert store.latest("ZZ_BUSY") is None


# ---------------------------------------------------------------------------
# Mode script: delegasikan ke pytest (file ini memang suite pytest).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
