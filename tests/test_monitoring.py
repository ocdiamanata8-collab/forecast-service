"""Pure logic + notifier tests for ``app.monitoring`` (7·04).

No live DB, webhook, or schedule required. Integration coverage for durable
state and SQL lives in ``tests/test_db_store.py`` (skipped without DB DSN).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import monitoring

UTC = timezone.utc
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
CONFIG = monitoring.MonitorConfig(failure_streak=3, stale_hours=3, repeat_hours=6)


def _run(status: str, hours_ago: float = 1, error: str | None = None):
    return (status, NOW - timedelta(hours=hours_ago), error)


def _evaluate(runs, *, last_ok=None, last_run=None, first_seen=None, now=NOW):
    return monitoring.evaluate_site(
        "PLTS_KUPANG",
        runs,
        last_success_at=last_ok,
        last_run_at=last_run,
        first_seen_at=first_seen or NOW - timedelta(hours=1),
        now=now,
        config=CONFIG,
    )


# ── consecutive-failure detection ────────────────────────────────────────
def test_two_consecutive_failures_do_not_alert():
    result = _evaluate([_run("failed", 1, "x"), _run("failed", 2, "x")])
    assert monitoring.ALERT_FAILURES not in result


def test_three_consecutive_failures_alert_at_threshold():
    result = _evaluate([
        _run("failed", 1, "x"), _run("failed", 2, "x"), _run("failed", 3, "x")
    ])
    assert monitoring.ALERT_FAILURES in result
    assert result[monitoring.ALERT_FAILURES].site_id == "PLTS_KUPANG"


def test_four_consecutive_failures_still_alert():
    result = _evaluate([_run("failed", h, "x") for h in (1, 2, 3, 4)])
    assert monitoring.ALERT_FAILURES in result


def test_success_resets_failure_streak():
    result = _evaluate([
        _run("failed", 1, "x"), _run("ok", 2), _run("failed", 3, "x"),
        _run("failed", 4, "x"), _run("failed", 5, "x"),
    ])
    assert monitoring.ALERT_FAILURES not in result


def test_failure_streak_runs_newest_first_and_stops_at_first_ok():
    runs = [_run("failed", 1), _run("failed", 2), _run("ok", 3), _run("failed", 4)]
    assert monitoring.failure_streak_from_runs(runs) == 2


def test_failure_streak_per_site_independent():
    runs_a = [_run("failed", 1), _run("failed", 2), _run("failed", 3)]
    runs_b = [_run("failed", 1), _run("ok", 2)]
    assert monitoring.ALERT_FAILURES in monitoring.evaluate_site(
        "A", runs_a, last_success_at=None, last_run_at=NOW - timedelta(hours=1),
        first_seen_at=NOW - timedelta(hours=1), now=NOW, config=CONFIG,
    )
    assert monitoring.ALERT_FAILURES not in monitoring.evaluate_site(
        "B", runs_b, last_success_at=NOW - timedelta(hours=2),
        last_run_at=NOW - timedelta(hours=1), first_seen_at=NOW - timedelta(hours=1),
        now=NOW, config=CONFIG,
    )


def test_unknown_run_status_fails_closed():
    with pytest.raises(ValueError, match="status run tidak dikenal"):
        _evaluate([_run("running")])


# ── stale / missed scheduler detection ───────────────────────────────────
def test_stale_not_alerted_before_three_hours():
    result = _evaluate(
        [_run("ok", 2.99)], last_ok=NOW - timedelta(hours=2, minutes=59),
        last_run=NOW - timedelta(hours=2, minutes=59),
    )
    assert monitoring.ALERT_STALE not in result


def test_stale_alerted_exactly_at_three_hours():
    result = _evaluate(
        [_run("ok", 3)], last_ok=NOW - timedelta(hours=3),
        last_run=NOW - timedelta(hours=3),
    )
    assert monitoring.ALERT_STALE in result


def test_stale_alerted_after_three_hours():
    result = _evaluate(
        [_run("ok", 4)], last_ok=NOW - timedelta(hours=4),
        last_run=NOW - timedelta(hours=4),
    )
    assert monitoring.ALERT_STALE in result


def test_without_success_fallback_to_latest_run_activity():
    # Run failed 2h ago: activity means not stale yet, despite no successful run.
    result = _evaluate(
        [_run("failed", 2, "x")], last_ok=None,
        last_run=NOW - timedelta(hours=2),
    )
    assert monitoring.ALERT_STALE not in result


def test_empty_run_history_uses_first_seen_grace():
    under = _evaluate([], last_ok=None, last_run=None,
                      first_seen=NOW - timedelta(hours=2, minutes=59))
    at = _evaluate([], last_ok=None, last_run=None,
                   first_seen=NOW - timedelta(hours=3))
    assert monitoring.ALERT_STALE not in under
    assert monitoring.ALERT_STALE in at


def test_stale_and_consecutive_failure_are_independent_conditions():
    result = _evaluate(
        [_run("failed", 4, "x"), _run("failed", 5, "x"), _run("failed", 6, "x")],
        last_ok=NOW - timedelta(hours=4), last_run=NOW - timedelta(hours=4),
    )
    assert set(result) == {monitoring.ALERT_FAILURES, monitoring.ALERT_STALE}


def test_naive_monitor_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        _evaluate([], last_ok=None, last_run=None, now=datetime(2026, 10, 27, 12))


# ── cooldown and recovery state machine ──────────────────────────────────
class MemoryMonitorStore:
    """Minimal duck-typed fake for ``_run_monitor`` state transitions."""

    _run_table_ok = True
    _alert_state_table_ok = True

    def __init__(self, *, runs=None, last_ok=None, last_run=None, first_seen=None):
        self.runs = list(runs or [])
        self.last_ok = last_ok
        self.last_run = last_run
        self.first_seen = first_seen or NOW - timedelta(hours=1)
        self.states = {}

    def alert_state_get(self, site, alert_type):
        row = self.states.get((site, alert_type))
        return dict(row) if row else None

    def alert_state_ensure(self, site, alert_type, first_seen):
        self.states.setdefault((site, alert_type), {
            "site_id": site,
            "alert_type": alert_type,
            "first_seen_at": self.first_seen if alert_type == monitoring.ALERT_STALE else first_seen,
            "active": False,
            "last_notified_at": None,
            "recovered_at": None,
        })

    def run_status_rows(self, site, limit):
        return self.runs[:limit]

    def last_successful_run_at(self, site):
        return self.last_ok

    def last_run_at(self, site):
        return self.last_run

    def alert_state_mark_notified(self, site, alert_type, *, now, cooldown_before):
        row = self.states[(site, alert_type)]
        if row["active"] and row["last_notified_at"] > cooldown_before:
            return False
        row["active"] = True
        row["last_notified_at"] = now
        row["recovered_at"] = None
        return True

    def alert_state_mark_recovered(self, site, alert_type, *, now):
        row = self.states[(site, alert_type)]
        if not row["active"]:
            return False
        row["active"] = False
        row["recovered_at"] = now
        return True


def _bad_store():
    runs = [_run("failed", 1, "x"), _run("failed", 2, "x"), _run("failed", 3, "x")]
    return MemoryMonitorStore(
        runs=runs, last_ok=NOW - timedelta(hours=1), last_run=NOW - timedelta(hours=1),
    )


def test_edge_trigger_sends_once_at_threshold(monkeypatch):
    sent = []
    monkeypatch.setattr(monitoring, "send_webhook", lambda _url, text: sent.append(text))
    store = _bad_store()
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                            config=CONFIG, webhook_url="https://secret.example/hook")
    assert len(sent) == 1
    assert "gagal beruntun" in sent[0]
    assert "secret.example" not in sent[0]
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW + timedelta(minutes=15),
                            config=CONFIG, webhook_url="https://secret.example/hook")
    assert len(sent) == 1


def test_repeat_only_after_six_hours(monkeypatch):
    sent = []
    monkeypatch.setattr(monitoring, "send_webhook", lambda _url, text: sent.append(text))
    store = _bad_store()
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                            config=CONFIG, webhook_url="https://secret.example/hook")
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW + timedelta(hours=5, minutes=59),
                            config=CONFIG, webhook_url="https://secret.example/hook")
    # Hanya alert "gagal beruntun" yang dihitung — kondisi stale ikut aktif
    # pada pemeriksaan kedua (umur sukses terakhir > 3 jam), dan itu perilaku
    # yang benar; yang diuji di sini adalah cooldown kondisi gagal beruntun.
    failure_alerts = [t for t in sent if "gagal beruntun" in t]
    assert len(failure_alerts) == 1
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW + timedelta(hours=6),
                            config=CONFIG, webhook_url="https://secret.example/hook")
    failure_alerts = [t for t in sent if "gagal beruntun" in t]
    assert len(failure_alerts) == 2
    assert failure_alerts[1].startswith("ALERT BERULANG")


def test_recovery_sent_once_after_condition_clears(monkeypatch):
    sent = []
    monkeypatch.setattr(monitoring, "send_webhook", lambda _url, text: sent.append(text))
    store = _bad_store()
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                            config=CONFIG, webhook_url="https://secret.example/hook")
    store.runs = [_run("ok", 0.1)]
    store.last_ok = NOW + timedelta(minutes=1)
    store.last_run = store.last_ok
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW + timedelta(minutes=2),
                            config=CONFIG, webhook_url="https://secret.example/hook")
    assert len(sent) == 2
    assert sent[1].startswith("PEMULIHAN")
    monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW + timedelta(minutes=17),
                            config=CONFIG, webhook_url="https://secret.example/hook")
    assert len(sent) == 2


def test_multiple_sites_and_alert_types_have_independent_state(monkeypatch):
    sent = []
    monkeypatch.setattr(monitoring, "send_webhook", lambda _url, text: sent.append(text))
    store = _bad_store()
    store.runs_by_site = {"PLTS_KUPANG": store.runs, "PLTS_ROTE": []}
    original = store.run_status_rows
    store.run_status_rows = lambda site, limit: store.runs_by_site[site][:limit]
    store.last_successful_run_at = lambda site: (
        NOW - timedelta(hours=4) if site == "PLTS_ROTE" else NOW - timedelta(hours=1)
    )
    store.last_run_at = lambda site: store.last_successful_run_at(site)
    store.first_seen = NOW - timedelta(hours=4)
    monitoring._run_monitor(store, sites=["PLTS_KUPANG", "PLTS_ROTE"], now=NOW,
                            config=CONFIG, webhook_url="https://secret.example/hook")
    assert len(sent) == 2
    assert any("PLTS_KUPANG" in text and "gagal beruntun" in text for text in sent)
    assert any("PLTS_ROTE" in text and "forecast basi" in text for text in sent)


def test_unconfigured_webhook_bad_condition_exits_nonzero_and_marks_cycle():
    """Tanpa webhook: Actions email = kanalnya.

    Job exit nonzero (email menyala) TAPI state tetap ditandai siklus ini —
    tanpa itu tiap polling 15 menit mengirim email baru (spam). Repeat baru
    sesudah cooldown, persis seperti mode webhook.
    """
    store = _bad_store()
    with pytest.raises(RuntimeError, match="WEBHOOK_URL tidak disetel"):
        monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                                config=CONFIG, webhook_url="")
    state = store.states[("PLTS_KUPANG", monitoring.ALERT_FAILURES)]
    assert state["active"] is True
    assert state["last_notified_at"] == NOW

    # Polling berikutnya dalam cooldown: kondisi masih buruk tapi SIKLUS
    # sudah diberitakan → tidak ada error berulang (exit 0).
    assert monitoring._run_monitor(
        store, sites=["PLTS_KUPANG"], now=NOW + timedelta(minutes=15),
        config=CONFIG, webhook_url="",
    ) == 0

    # Cooldown lewat: notifikasi ulang lewat Actions (error lagi).
    with pytest.raises(RuntimeError, match="WEBHOOK_URL tidak disetel"):
        monitoring._run_monitor(store, sites=["PLTS_KUPANG"],
                                now=NOW + timedelta(hours=6),
                                config=CONFIG, webhook_url="")


def test_webhook_failure_does_not_update_state(monkeypatch):
    store = _bad_store()
    monkeypatch.setattr(monitoring, "send_webhook", lambda *_: (_ for _ in ()).throw(
        RuntimeError("pengiriman webhook gagal (URL dirahasiakan)")))
    with pytest.raises(RuntimeError, match="pengiriman webhook gagal"):
        monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                                config=CONFIG, webhook_url="https://secret.example/hook")
    assert not store.states[("PLTS_KUPANG", monitoring.ALERT_FAILURES)]["active"]
    assert store.states[("PLTS_KUPANG", monitoring.ALERT_FAILURES)]["last_notified_at"] is None


def test_monitor_refuses_missing_run_log_or_alert_state():
    store = MemoryMonitorStore()
    store._run_table_ok = False
    with pytest.raises(RuntimeError, match="forecast_run"):
        monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                                config=CONFIG, webhook_url="")
    store._run_table_ok = True
    store._alert_state_table_ok = False
    with pytest.raises(RuntimeError, match="forecast_alert_state"):
        monitoring._run_monitor(store, sites=["PLTS_KUPANG"], now=NOW,
                                config=CONFIG, webhook_url="")


# ── notifier, environment config, CLI ────────────────────────────────────
def test_send_webhook_posts_generic_text_json(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: (calls.append((a, kw)) or Response()))
    monitoring.send_webhook("https://secret.example/hook", "ALERT site=PLTS_KUPANG")
    assert calls[0][0][0] == "https://secret.example/hook"
    assert calls[0][1]["json"] == {"text": "ALERT site=PLTS_KUPANG"}
    assert calls[0][1]["timeout"] == monitoring.WEBHOOK_TIMEOUT_SECONDS


def test_send_webhook_sanitizes_http_errors(monkeypatch, caplog):
    def fail(*_a, **_kw):
        raise httpx.ConnectError("failed https://secret.example/hook")

    monkeypatch.setattr(httpx, "post", fail)
    with pytest.raises(RuntimeError) as exc:
        monitoring.send_webhook("https://secret.example/hook", "test")
    assert "secret.example" not in str(exc.value)
    assert "secret.example" not in caplog.text


def test_parse_config_defaults_and_rejects_invalid():
    assert monitoring._parse_config({}) == CONFIG
    with pytest.raises(ValueError):
        monitoring._parse_config({"FORECAST_ALERT_FAILURE_STREAK": "0"})
    with pytest.raises(ValueError):
        monitoring._parse_config({"FORECAST_ALERT_STALE_HOURS": "abc"})


def test_main_without_database_url_fails(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert monitoring._main() == 1
    assert "DATABASE_URL tidak disetel" in capsys.readouterr().out
