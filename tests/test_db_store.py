"""Uji store PostgreSQL terhadap database NYATA (Fase 6 · 6·02).

Jalur: ``python -m pytest tests/test_db_store.py -q``

Test ini melewati (skip) bila ``DATABASE_URL`` tidak disetel — kredensial DB
milik environment, tidak boleh ditulis ke kode/repo. Menjalankan seluruh
siklus yang dijanjikan 6·02:

- latest MEMBACA baris tersimpan (tanpa komputasi forecast),
- write-once: save ulang batch sama idempoten, tidak menimpa,
- dua batch berbeda → latest memilih ``generated_at`` terbesar,
- urutan history ``generated_at DESC``, tie-break ``forecast_id ASC``,
- offset ``+08:00`` dipertahankan di seluruh timestamp (§1.5),
- cursor pagination + deteksi filter berubah (§5.3),
- ``E_NO_FORECAST`` path: site tanpa baris → None.

Tabel yang dipakai: migration 6·01 (``forecast_result``). Test memakai
site_id sintetis ``ZZ_TEST_DB`` + generated_at khas supaya baris uji mudah
dibersihkan dan tidak bentrok dengan data PLTS_KUPANG.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL", "").strip()
    ),
    reason="DATABASE_URL tidak disetel — test DB live melewati "
           "(set env untuk menjalankan; conftest memindahkannya ke "
           "TEST_DATABASE_URL agar lifespan memilih store in-memory)",
)

#: DSN live — conftest mengangkat DATABASE_URL agar suite lain hermetic.
#: ``or ""`` supaya import modul tidak KeyError saat env kosong (default;
#: pytestmark di atas sudah melewatkan seluruh test pada kondisi ini).
_DSN = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

from app.db import DatabaseUnavailable, PostgresForecastStore  # noqa: E402
from app.schemas import ForecastPoint  # noqa: E402
from app.storage import ForecastRunBusy, StoredForecast  # noqa: E402

WITA = timezone(timedelta(hours=8))
SITE = "ZZ_TEST_DB"


def _batch(generated_at: datetime, *, points: int = 3, value: float = 100.0):
    """Bangun StoredForecast sintetis (horizon = points, step 1-based)."""
    fid = str(uuid4())
    return StoredForecast(
        forecast_id=fid,
        site_id=SITE,
        target="irradiance",
        resolution="daily",
        unit="W/m2",
        ts=generated_at,
        generated_at=generated_at,
        horizon=points,
        model_version="v0.0.1",
        forecast=tuple(
            ForecastPoint(
                forecast_time=generated_at + timedelta(days=i),
                forecast_value=value + i,
                model_version="v0.0.1",
            )
            for i in range(points)
        ),
    ), fid


@pytest.fixture()
def store():
    s = PostgresForecastStore(_DSN)
    yield s
    # bersihkan baris uji (termasuk site run-gagal) agar run berikutnya
    # deterministik — run log TIDAK pernah ditimpa (6·03), jadi akumulasi
    # antar-test akan merusak asersi "satu run = satu baris".
    with s._pool.connection() as conn:
        conn.execute("DELETE FROM forecast_run WHERE site_id IN (%s, %s)",
                     (SITE, "ZZ_GAGAL"))
        conn.execute("DELETE FROM forecast_result WHERE site_id = %s", (SITE,))
        # State alert ikut dibersihkan: kuncinya (site, alert_type) dan
        # monitor membaca state lama → hasil antar-test bisa bocor.
        conn.execute("DELETE FROM forecast_alert_state WHERE site_id IN (%s, %s)",
                     (SITE, "ZZ_GAGAL"))
    s.close()


def test_latest_reads_persisted_rows_not_recompute(store):
    """6·02: latest mengembalikan PERSIS baris yang disimpan — bukan hasil
    komputasi ulang. Bukti: nilai forecast_value identik sampai koma."""
    item, _fid = _batch(datetime(2026, 10, 19, 6, 0, tzinfo=WITA), value=42.5)
    store.save(item)

    got = store.latest(SITE)
    assert got is not None
    assert got.forecast_id == item.forecast_id
    assert [p.forecast_value for p in got.forecast] == [
        p.forecast_value for p in item.forecast
    ]
    assert [p.forecast_time for p in got.forecast] == [
        p.forecast_time for p in item.forecast
    ]
    # §1.5: offset WITA utuh setelah melalui timestamptz
    assert got.generated_at.utcoffset() == timedelta(hours=8)
    assert all(p.forecast_time.utcoffset() == timedelta(hours=8) for p in got.forecast)


def test_write_once_idempotent_same_batch(store):
    """Save ulang batch identik TIDAK menggandakan & tidak menimpa."""
    item, fid = _batch(datetime(2026, 10, 19, 7, 0, tzinfo=WITA))
    store.save(item)
    store.save(item)  # idempoten
    with store._pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM forecast_result WHERE forecast_id = %s",
            (fid,),
        ).fetchone()[0]
    assert n == item.horizon  # tetap 1 baris per titik


def test_latest_picks_newest_generated_at(store):
    older, _ = _batch(datetime(2026, 10, 18, 6, 0, tzinfo=WITA), value=10.0)
    newer, _ = _batch(datetime(2026, 10, 19, 6, 0, tzinfo=WITA), value=99.0)
    store.save(older)
    store.save(newer)
    got = store.latest(SITE)
    assert got is not None
    assert got.generated_at == newer.generated_at
    assert got.forecast[0].forecast_value == 99.0


def test_latest_empty_site_returns_none(store):
    assert store.latest("ZZ_SITE_TIDAK_ADA") is None


def test_history_order_and_cursor(store):
    """§5.3: generated_at DESC + tie-break forecast_id ASC; cursor lanjut."""
    base = datetime(2026, 10, 19, 6, 0, tzinfo=WITA)
    items = []
    for day in range(4):
        it, _ = _batch(base + timedelta(days=day))
        store.save(it)
        items.append(it)

    page1, cur1 = store.history(site_id=SITE, limit=3)
    assert [i.generated_at for i in page1] == sorted(
        [i.generated_at for i in page1], reverse=True
    )
    assert len(page1) == 3
    assert cur1 is not None  # ada halaman berikut (4 batch tersimpan)

    page2, cur2 = store.history(site_id=SITE, limit=3, cursor=cur1)
    assert cur2 is None
    # tidak ada duplikat antar halaman
    ids1 = {i.forecast_id for i in page1}
    ids2 = {i.forecast_id for i in page2}
    assert ids1 & ids2 == set()
    assert ids1 | ids2 == {i.forecast_id for i in items}


def test_history_filter_from_to(store):
    base = datetime(2026, 10, 19, 6, 0, tzinfo=WITA)
    for day in range(3):
        it, _ = _batch(base + timedelta(days=day))
        store.save(it)
    got, _ = store.history(
        site_id=SITE,
        start=base + timedelta(days=1),
        end=base + timedelta(days=1, hours=23),
        limit=10,
    )
    assert len(got) == 1
    assert got[0].generated_at == base + timedelta(days=1)


def test_store_fails_fast_without_table():
    """DSN yang terjangkau tapi tabel belum ada → DatabaseUnavailable (fast),
    BUKAN fallback diam-diam ke in-memory (prinsip app/db.py)."""
    dsn = os.environ.get("DATABASE_URL_TEST_EMPTY", "").strip()
    if not dsn:
        pytest.skip("DATABASE_URL_TEST_EMPTY tidak disetel")
    with pytest.raises(DatabaseUnavailable):
        PostgresForecastStore(dsn)


# ===========================================================================
# 6·03 — run log penjadwalan (forecast_run)
# ===========================================================================
def _runs(store, site: str) -> list[tuple]:
    with store._pool.connection() as conn:
        return conn.execute(
            "SELECT site_id, status, rows_saved, forecast_id, "
            "       generated_at, model_version, error "
            "  FROM forecast_run WHERE site_id = %s "
            " ORDER BY started_at ASC",
            (site,),
        ).fetchall()


def _insert_run(store, site: str, status: str, started_at: datetime, error=None):
    store.record_run(
        site_id=site,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        status=status,
        error=error,
    )


def test_monitor_run_status_query_orders_newest_first(store):
    """DB read API returns run log descending by started_at + deterministic id."""
    from datetime import timezone as _tz

    base = datetime(2026, 10, 20, 6, 0, tzinfo=_tz(timedelta(hours=8)))
    for offset, status in enumerate(("ok", "failed", "failed", "failed")):
        _insert_run(store, SITE, status, base + timedelta(hours=offset),
                    error="test" if status == "failed" else None)
    rows = store.run_status_rows(SITE, limit=3)
    assert [row[0] for row in rows] == ["failed", "failed", "failed"]
    assert rows == sorted(rows, key=lambda row: row[1], reverse=True)
    assert store.last_successful_run_at(SITE) == base
    assert store.last_run_at(SITE) == base + timedelta(hours=3)


def test_alert_state_upsert_is_durable_and_idempotent(store):
    """State alert per site/type survives read/write and upsert."""
    from datetime import timezone as _tz

    seen = datetime(2026, 10, 20, 6, 0, tzinfo=_tz(timedelta(hours=8)))
    sent = seen + timedelta(hours=3)
    store.alert_state_ensure(SITE, "consecutive_failure", seen)
    first = store.alert_state_get(SITE, "consecutive_failure")
    assert first is not None and first["active"] is False
    assert first["first_seen_at"] == seen

    assert store.alert_state_mark_notified(
        SITE, "consecutive_failure", now=sent,
        cooldown_before=sent - timedelta(hours=6),
    ) is True
    active = store.alert_state_get(SITE, "consecutive_failure")
    assert active["active"] is True
    assert active["last_notified_at"] == sent

    # Ulangi ensure tidak menimpa first_seen_at/active (idempotent).
    store.alert_state_ensure(SITE, "consecutive_failure", seen + timedelta(days=1))
    after = store.alert_state_get(SITE, "consecutive_failure")
    assert after["first_seen_at"] == seen
    assert after["active"] is True

    # Active + cooldown belum lewat → CAS guard menolak notifikasi duplikat.
    assert store.alert_state_mark_notified(
        SITE, "consecutive_failure", now=sent + timedelta(hours=1),
        cooldown_before=sent - timedelta(hours=5),
    ) is False
    # Tepat setelah cooldown → notifikasi berulang boleh.
    repeat_at = sent + timedelta(hours=6)
    assert store.alert_state_mark_notified(
        SITE, "consecutive_failure", now=repeat_at,
        cooldown_before=sent,
    ) is True
    assert store.alert_state_get(SITE, "consecutive_failure")["last_notified_at"] == repeat_at

    assert store.alert_state_mark_recovered(
        SITE, "consecutive_failure", now=repeat_at + timedelta(minutes=1)
    ) is True
    recovered = store.alert_state_get(SITE, "consecutive_failure")
    assert recovered["active"] is False
    assert recovered["recovered_at"] == repeat_at + timedelta(minutes=1)
    assert store.alert_state_mark_recovered(
        SITE, "consecutive_failure", now=repeat_at + timedelta(minutes=2)
    ) is False


def test_site_lock_key_deterministic_and_fits_bigint():
    """Kunci lintas proses: deterministik (bukan hash() yang di-random) dan
    muat di bigint (advisory lock satu-argumen)."""
    from app.db import _site_lock_key

    assert _site_lock_key(SITE) == _site_lock_key(SITE)
    assert _site_lock_key(SITE) != _site_lock_key("ZZ_LAIN")
    for candidate in (SITE, "PLTS_KUPANG", "x" * 300, "unicode-✓-site"):
        assert -2**63 <= _site_lock_key(candidate) < 2**63


def test_site_run_lock_blocks_second_trigger_and_releases(store):
    """Pemicu kedua pada site sama DITOLAK (bukan antre); setelah lepas,
    kunci bisa dipegang lagi — tidak ada kunci yang menggantung."""
    other = PostgresForecastStore(_DSN)
    try:
        with store.site_run_lock(SITE):
            with pytest.raises(ForecastRunBusy):
                with other.site_run_lock(SITE):
                    pass  # pragma: no cover — tidak boleh tercapai
        # Kunci dilepas setelah konteks → pemicu kedua boleh masuk.
        with other.site_run_lock(SITE):
            pass
    finally:
        other.close()


def test_site_run_lock_is_per_site_not_global(store):
    """Site berbeda punya kunci berbeda — lock satu site tidak memblokir
    site lain (penting bila SITE_IDS > 1)."""
    other = PostgresForecastStore(_DSN)
    try:
        with store.site_run_lock(SITE):
            with other.site_run_lock("ZZ_TEST_DB_LAIN"):
                pass  # site lain bebas — tak terblokir
    finally:
        other.close()


def test_site_run_lock_survives_pool_reuse(store):
    """Kunci tidak bocor ke koneksi pool berikutnya: siklus berulang melebihi
    ukuran pool (min 1 / max 4) — tiap siklus membuang sesi berkunci lalu
    memakai sesi baru; kunci lama wajib sudah mati."""
    for i in range(12):
        with store.site_run_lock(SITE):
            pass
    with store.site_run_lock(SITE):  # masih bisa kunci — sesi lama tak menggantung
        pass


def test_run_site_forecast_busy_when_other_trigger_holds_lock(store, monkeypatch):
    """End-to-end: run_site_forecast melempar ForecastRunBusy TANPA mencatat
    run gagal — "sibuk" bukan kegagalan (mencatatnya memicu alarm alert 7·04
    palsu)."""
    import numpy as np
    import pandas as pd

    from app import scheduler
    from app.preprocessing import DEFAULT_TIMEZONE

    idx = pd.date_range("2024-01-01", periods=90, freq="D", tz=DEFAULT_TIMEZONE)
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    hist = [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]
    monkeypatch.setattr(scheduler, "fetch_history", lambda _s, **_kw: hist)

    other = PostgresForecastStore(_DSN)
    try:
        with other.site_run_lock(SITE):
            with pytest.raises(ForecastRunBusy, match="berjalan"):
                scheduler.run_site_forecast(store, SITE, horizon=7)
            # Tidak ada run tercatat (bukan kegagalan) & tidak ada batch.
            assert _runs(store, SITE) == []
            assert store.latest(SITE) is None
        # Setelah kunci lepas: run normal, tercatat sebagai ok.
        item = scheduler.run_site_forecast(store, SITE, horizon=7)
        rows = _runs(store, SITE)
        assert len(rows) == 1 and rows[0][1] == "ok"
        assert str(rows[0][3]) == item.forecast_id
    finally:
        other.close()


def test_run_log_records_success_and_failure(store, monkeypatch):
    """Run sukses & gagal keduanya tercatat; sukses menunjuk batch-nya.

    Jejak run dicatat DI DALAM ``run_site_forecast`` (bukan di ``tick``), jadi
    test ini mem-patch ``fetch_history`` — bukan ``run_site_forecast`` — agar
    jalur pencatatan yang asli ikut teruji.
    """
    import numpy as np
    import pandas as pd

    from app import scheduler
    from app.preprocessing import DEFAULT_TIMEZONE

    idx = pd.date_range("2024-01-01", periods=90, freq="D", tz=DEFAULT_TIMEZONE)
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    hist = [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]

    def fake_fetch(site_id, **_kw):
        if site_id == "ZZ_GAGAL":
            raise RuntimeError("sintetis: actuals hilang")
        return hist

    monkeypatch.setattr(scheduler, "fetch_history", fake_fetch)
    saved = scheduler.tick(store, ["ZZ_GAGAL", SITE])
    assert len(saved) == 1  # hanya site sukses

    rows = _runs(store, SITE)
    assert len(rows) == 1
    site, status, rows_saved, fid, gen_at, mv, err = rows[0]
    assert status == "ok"
    assert rows_saved == scheduler.DEFAULT_HORIZON
    assert fid is not None and str(fid) == saved[0]
    assert gen_at is not None
    assert mv is not None
    assert err is None

    # run gagal tercatat TERPISAH dengan error
    fail_rows = _runs(store, "ZZ_GAGAL")
    assert len(fail_rows) == 1
    assert fail_rows[0][1] == "failed"
    assert "sintetis" in (fail_rows[0][6] or "")


def test_run_site_forecast_records_run_without_tick(store, monkeypatch):
    """``run_site_forecast`` sendiri mencatat run — jalur endpoint job & cron
    eksternal memanggil fungsi itu LANGSUNG (tanpa ``tick``), jadi jejak run
    harus ada di sana juga; kalau tidak, run di luar aplikasi tak terlihat.
    """
    import numpy as np
    import pandas as pd

    from app import scheduler
    from app.preprocessing import DEFAULT_TIMEZONE

    idx = pd.date_range("2024-01-01", periods=90, freq="D", tz=DEFAULT_TIMEZONE)
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    hist = [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]

    monkeypatch.setattr(scheduler, "fetch_history", lambda _site, **_kw: hist)
    item = scheduler.run_site_forecast(store, SITE, horizon=scheduler.DEFAULT_HORIZON)

    rows = _runs(store, SITE)
    assert len(rows) == 1, "run langsung harus tercatat seperti run via tick"
    site, status, rows_saved, fid, gen_at, mv, err = rows[0]
    assert status == "ok"
    assert rows_saved == scheduler.DEFAULT_HORIZON
    assert str(fid) == item.forecast_id
    assert gen_at is not None
    assert err is None


def test_each_run_writes_its_own_generated_at(store, monkeypatch):
    """Dua run → dua batch dengan generated_at sendiri (riwayat run tidak
    ditimpa) — dasar mengukur perbaikan forecast antar run."""
    import numpy as np
    import pandas as pd

    from app import scheduler
    from app.preprocessing import DEFAULT_TIMEZONE

    idx = pd.date_range("2024-01-01", periods=90, freq="D", tz=DEFAULT_TIMEZONE)
    day = idx.dayofyear.to_numpy(dtype="float64")
    values = np.clip(180 + 60 * np.sin(2 * np.pi * day / 365.25), 0, None)
    hist = [(ts.to_pydatetime(), float(v)) for ts, v in zip(idx, values)]

    monkeypatch.setattr(scheduler, "fetch_history", lambda _site, **_kw: hist)
    scheduler.tick(store, [SITE])
    scheduler.tick(store, [SITE])

    rows = _runs(store, SITE)
    assert len(rows) == 2, "setiap run = satu baris run log (tidak ditimpa)"
    gens = {r[4] for r in rows}
    assert len(gens) == 2, "tiap run punya generated_at sendiri"


# ===========================================================================
# 6·04 — backfill aktual (actual_value diisi menyusul)
# ===========================================================================
def test_backfill_fills_only_past_rows(store):
    """Baris dengan forecast_time < cutoff terisi dari actual_result;
    baris masa depan tetap NULL ("menunggu pengamatan")."""
    from datetime import timezone as _tz

    from app import backfill

    now = datetime(2026, 10, 19, 12, 0, tzinfo=_tz(timedelta(hours=8)))
    item, _fid = _batch(
        datetime(2026, 10, 17, 6, 0, tzinfo=WITA),
        points=5, value=200.0,
    )  # forecast_time: 17,18,19,20,21 Okt
    store.save(item)

    summary = backfill.backfill_once(store, sites=["PLTS_KUPANG"], cutoff=now)
    assert summary["synced"] > 0, "deret aktual (data/actuals.csv) harus tersinkron"
    # dataset berakhir 31-12-2025 → titik 17..21 Okt 2026 TIDAK ada aktualnya
    assert summary["filled"] == 0

    with store._pool.connection() as conn:
        n_past = conn.execute(
            "SELECT count(*) FROM forecast_result "
            "WHERE forecast_time < %s AND actual_value IS NOT NULL",
            (now,),
        ).fetchone()[0]
        n_future = conn.execute(
            "SELECT count(*) FROM forecast_result "
            "WHERE forecast_time >= %s AND actual_value IS NOT NULL",
            (now,),
        ).fetchone()[0]
    # cutoff lewat horizon → semua titik < cutoff, tapi tanpa pasangan
    # aktual (2026) → belum terisi. Sekarang uji jalur ISI dengan cutoff
    # di rentang dataset: lihat test berikutnya.
    assert n_future == 0, "baris masa depan tidak boleh diisi (menunggu pengamatan)"
    del n_past


def test_backfill_fills_when_actual_available(store):
    """Siklus lengkap: forecast pada rentang dataset (2025) → setelah
    backfill, actual_value terisi — inilah bahan akurasi Fase 8."""
    from datetime import timezone as _tz

    from app import backfill

    now = datetime(2026, 1, 10, 0, 0, tzinfo=_tz(timedelta(hours=8)))
    # batch diakhiri ts=2025-12-31; forecast_time 01..05 Jan 2026
    # → analisis: dataset berakhir 31-12-2025, jadi pakai batch lebih lama
    item, _fid = _batch(
        # tengah malam WITA — sejajar index harian actuals (dataset kalender)
        datetime(2025, 12, 20, 0, 0, tzinfo=WITA),
        points=5, value=200.0,
    )  # forecast_time: 21..25 Des 2025 — ada di actuals.csv
    store.save(item)

    summary = backfill.backfill_once(store, sites=[SITE], cutoff=now)
    assert summary["filled"] > 0, "baris pada rentang data wajib terisi aktualnya"

    with store._pool.connection() as conn:
        n_filled = conn.execute(
            "SELECT count(*) FROM forecast_result "
            "WHERE forecast_id = %s AND actual_value IS NOT NULL",
            (_fid,),
        ).fetchone()[0]
        sample = conn.execute(
            "SELECT forecast_time, actual_value FROM forecast_result "
            "WHERE forecast_id = %s ORDER BY horizon_step LIMIT 1",
            (_fid,),
        ).fetchone()
    assert n_filled == item.horizon
    assert sample[1] is not None and float(sample[1]) >= 0


def test_backfill_never_overwrites_filled_value(store):
    """6·04: "diisi menyusul" — baris yang SUDAH terisi tidak pernah diubah,
    walau sinkronisasi berikutnya mengirim nilai berbeda."""
    from datetime import timezone as _tz

    from app import backfill

    from app.schemas import ForecastPoint
    from app.storage import StoredForecast

    ts = datetime(2025, 6, 15, 0, 0, tzinfo=WITA)
    past = datetime(2025, 6, 20, 0, 0, tzinfo=_tz(timedelta(hours=8)))
    item = StoredForecast(
        forecast_id=str(__import__("uuid").uuid4()),
        site_id=SITE, target="irradiance", resolution="daily", unit="W/m2",
        ts=ts, generated_at=ts, horizon=1, model_version="v0.0.1",
        forecast=(ForecastPoint(
            forecast_time=ts + timedelta(days=1), forecast_value=100.0,
            model_version="v0.0.1"),),
    )
    store.save(item)
    backfill.backfill_once(store, sites=[SITE], cutoff=past)

    with store._pool.connection() as conn:
        before = conn.execute(
            "SELECT actual_value FROM forecast_result WHERE forecast_id = %s",
            (item.forecast_id,),
        ).fetchone()[0]
        # Dorong nilai palsu ke actual_result di TITIK YANG SAMA dengan
        # forecast_time baris di atas — siklus berikutnya tidak boleh
        # mengubah baris forecast yang sudah terisi.
        conn.execute(
            "UPDATE actual_result SET actual_value = 999999.0 "
            "WHERE site_id = %s AND ts = %s",
            (SITE, item.forecast[0].forecast_time),
        )
    backfill.backfill_once(store, sites=[SITE], cutoff=past)
    with store._pool.connection() as conn:
        after = conn.execute(
            "SELECT actual_value FROM forecast_result WHERE forecast_id = %s",
            (item.forecast_id,),
        ).fetchone()[0]
        # bersihkan nilai uji agar sinkron berikutnya pulih
        conn.execute(
            "UPDATE actual_result SET actual_value = NULL "
            "WHERE site_id = %s AND ts = %s AND actual_value = 999999.0",
            (SITE, item.forecast[0].forecast_time),
        )
    assert float(after) == float(before), (
        "baris forecast yang sudah terisi tidak boleh ditimpa ulang"
    )


def test_backfill_once_raises_on_actuals_failure(store, monkeypatch):
    """Kegagalan CSV pada satu-shot backfill TERLIHAT (exception), bukan hilang.

    Loop internal sudah dihapus — backfill kini job sekali-jalan
    (``python -m app.backfill``) yang exit nonzero saat gagal, sehingga
    kegagalan terlihat di log runner CI, bukan tenggelam di dalam loop.
    """
    from app import actuals as actual_data
    from app import backfill
    from app.actuals import ActualsUnavailable

    def boom(**_kw):
        raise ActualsUnavailable("CSV hilang")

    monkeypatch.setattr(actual_data, "load_actual_series", boom)

    with pytest.raises(ActualsUnavailable):
        backfill.backfill_once(store, sites=[SITE])


# ===========================================================================
# 6·06 — retensi & backup
# ===========================================================================
def test_retention_default_keeps_everything(store):
    """Default 0 hari = TIDAK MENGHAPUS APA PUN (bukti akurasi, volume kecil)."""
    from datetime import timezone as _tz

    from app import retention

    item, _ = _batch(datetime(2020, 1, 1, 6, 0, tzinfo=WITA))
    store.save(item)

    assert retention.retention_days_from_env({}) == 0
    summary = store.prune_forecasts(older_than_days=0)
    assert summary == {"rows": 0, "runs": 0, "actuals": 0}
    assert store.latest(SITE) is not None, "tanpa retensi aktif, data tetap utuh"


def test_retention_prunes_old_batches_whole(store):
    """Retensi aktif menghapus batch TUA utuh, mempertahankan yang baru."""
    from datetime import timezone as _tz

    old, _ = _batch(datetime(2020, 1, 1, 6, 0, tzinfo=WITA), points=3)
    new, _ = _batch(datetime(2026, 10, 18, 6, 0, tzinfo=WITA), points=3)
    store.save(old)
    store.save(new)

    summary = store.prune_forecasts(older_than_days=365)
    assert summary["rows"] == old.horizon  # seluruh batch tua terhapus
    assert store.latest(SITE) is not None  # batch baru selamat
    with store._pool.connection() as conn:
        # batch TUA hilang UTUH — tidak menyisakan baris yatim
        orphan = conn.execute(
            "SELECT count(*) FROM forecast_result "
            "WHERE forecast_id = %s AND forecast_time IS NOT NULL",
            (old.forecast_id,),
        ).fetchone()[0]
    assert orphan == 0, "batch harus terhapus utuh, bukan baris per baris"


def test_retention_keeps_actuals_when_prune_actuals_false(store):
    """Default retensi TIDAK menyentuh deret pengamatan (pembanding lintas-model)."""
    from datetime import timezone as _tz

    from app import backfill

    now = datetime(2026, 10, 19, 12, 0, tzinfo=_tz(timedelta(hours=8)))
    backfill.backfill_once(store, sites=[SITE], cutoff=now)
    with store._pool.connection() as conn:
        before = conn.execute(
            "SELECT count(*) FROM actual_result WHERE site_id = %s", (SITE,)
        ).fetchone()[0]
    assert before > 0

    store.prune_forecasts(older_than_days=0)  # mati
    store.prune_forecasts(older_than_days=365, prune_actuals=False)
    with store._pool.connection() as conn:
        after = conn.execute(
            "SELECT count(*) FROM actual_result WHERE site_id = %s", (SITE,)
        ).fetchone()[0]
    assert after == before, "deret aktual tidak boleh terhapus saat prune_actuals=False"


def test_backup_script_roundtrip(tmp_path):
    """6·06: script backup menghasilkan file dump yang bisa dipakai ulang."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL tidak disetel")

    out = tmp_path / "backups"
    env = dict(os.environ)
    env["DATABASE_URL"] = dsn
    proc = subprocess.run(
        [sys.executable, "scripts/backup_db.py", "--out", str(out),
         "--via-docker", "--container", "forecast-pg"],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    dumps = list(out.glob("forecast-*.dump.gz"))
    assert len(dumps) == 1, "satu file backup harus terbentuk"
    assert dumps[0].stat().st_size > 0
    # restore uji: dump ter-decompress gzip lalu dibaca pg_restore (format valid)
    import gzip
    with gzip.open(dumps[0], "rb") as fh:
        dump_bytes = fh.read()
    assert dump_bytes[:5] == b"PGDMP", "isi harus format custom pg_dump"
    restore = subprocess.run(
        ["docker", "exec", "-i", "forecast-pg", "pg_restore", "--list"],
        input=dump_bytes, capture_output=True, timeout=120,
    )
    assert restore.returncode == 0, restore.stderr.decode(errors="replace")[:300]
    text = restore.stdout.decode(errors="replace")
    assert "forecast_result" in text and "forecast_run" in text
