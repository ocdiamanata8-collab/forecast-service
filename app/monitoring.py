"""Monitor kegagalan forecast — CLI satu-shot untuk scheduler eksternal (7·04).

Dua kondisi yang dievaluasi secara INDEPENDEN per site:

* ``consecutive_failure`` — tiga (default) run terbaru berturut-turut ``failed``.
* ``stale_run`` — tidak ada run sukses selama tiga jam (termasuk cron yang
  tidak berjalan sama sekali dan karenanya tidak menghasilkan row gagal).

Dijalankan tiap 15 menit oleh ``.github/workflows/forecast-monitor.yml``. State
alert durable disimpan di PostgreSQL agar polling tidak mengirim spam: notifikasi
awal saat ambang terlewati, pengulangan maksimal tiap enam jam, satu notifikasi
pemulihan. State ``active`` hanya diperbarui SETELAH webhook sukses; webhook
mati → pemeriksaan exit nonzero, GitHub Actions mengirim notifikasi kegagalan,
dan poll berikutnya mencoba lagi.

Tidak ada FastAPI/endpoint: monitor bukan bagian dari jalur layanan. Tanpa
``DATABASE_URL``, migrasi 002/003, atau bila DB tak terjangkau, monitor exit 1
(sehat palsu lebih berbahaya daripada pemeriksaan merah).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import httpx

log = logging.getLogger("app.monitoring")

ALERT_FAILURES = "consecutive_failure"
ALERT_STALE = "stale_run"
ALERT_TYPES = (ALERT_FAILURES, ALERT_STALE)
DEFAULT_FAILURE_STREAK = 3
DEFAULT_STALE_HOURS = 3
DEFAULT_REPEAT_HOURS = 6
DEFAULT_RUNS_TO_READ = 20
WEBHOOK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class MonitorConfig:
    """Ambang monitor; config diuji murni tanpa DB atau network."""

    failure_streak: int = DEFAULT_FAILURE_STREAK
    stale_hours: float = DEFAULT_STALE_HOURS
    repeat_hours: float = DEFAULT_REPEAT_HOURS

    def __post_init__(self) -> None:
        if self.failure_streak < 1:
            raise ValueError("failure_streak harus >= 1")
        if self.stale_hours <= 0:
            raise ValueError("stale_hours harus > 0")
        if self.repeat_hours <= 0:
            raise ValueError("repeat_hours harus > 0")


@dataclass(frozen=True)
class Condition:
    """Kondisi buruk yang terdeteksi untuk satu site."""

    site_id: str
    alert_type: str
    detail: str


def _aware(value: datetime | None) -> datetime | None:
    """Normalisasi waktu DB/tes ke UTC; tolak datetime naive (zona jangan ditebak)."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp monitor wajib timezone-aware")
    return value.astimezone(timezone.utc)


def evaluate_site(
    site_id: str,
    runs: Sequence[tuple],
    *,
    last_success_at: datetime | None,
    last_run_at: datetime | None,
    first_seen_at: datetime,
    now: datetime,
    config: MonitorConfig = MonitorConfig(),
) -> dict[str, Condition]:
    """Evaluasi dua kondisi buruk site. Input run urut newest-first.

    ``runs`` berisi tuple ``(status, started_at, error)``. Berhenti menghitung
    streak pada run sukses pertama. Stale dihitung dari sukses terakhir; bila
    belum pernah sukses, run terakhir (gagal pun activity) dipakai. Bila belum
    pernah ada run, grace period dimulai dari ``first_seen_at`` yang durable.
    Tepat pada batas ambang = alert.
    """
    now_utc = _aware(now)
    assert now_utc is not None
    first_seen = _aware(first_seen_at)
    assert first_seen is not None

    result: dict[str, Condition] = {}

    streak = failure_streak_from_runs(runs)
    if streak >= config.failure_streak:
        result[ALERT_FAILURES] = Condition(
            site_id=site_id,
            alert_type=ALERT_FAILURES,
            detail=f"{streak} kegagalan berturut-turut (ambang {config.failure_streak})",
        )

    last_ok = _aware(last_success_at)
    activity = last_ok or _aware(last_run_at) or first_seen
    stale_after = timedelta(hours=config.stale_hours)
    age = now_utc - activity
    if age >= stale_after:
        hours = age.total_seconds() / 3600
        result[ALERT_STALE] = Condition(
            site_id=site_id,
            alert_type=ALERT_STALE,
            detail=(f"tidak ada run sukses selama {hours:.1f} jam "
                    f"(ambang {config.stale_hours:g} jam)"),
        )
    return result


def failure_streak_from_runs(runs: Sequence[tuple]) -> int:
    """Jumlah run ``failed`` beruntun dari paling baru sampai ``ok`` pertama."""
    streak = 0
    for status, _started_at, _error in runs:
        if status == "ok":
            break
        if status != "failed":
            raise ValueError(f"status run tidak dikenal: {status!r}")
        streak += 1
    return streak


def send_webhook(url: str, text: str) -> None:
    """Kirim payload generik ``{"text": ...}``; jangan pernah log URL/secret."""
    if not url or not url.strip():
        raise RuntimeError("FORECAST_ALERT_WEBHOOK_URL tidak disetel")
    try:
        response = httpx.post(
            url,
            json={"text": text},
            timeout=WEBHOOK_TIMEOUT_SECONDS,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
    except Exception:
        # Exception httpx dapat memuat URL webhook (credential) di repr —
        # ganti dengan pesan generik dan hilangkan chained exception.
        raise RuntimeError("pengiriman webhook gagal (URL dirahasiakan)") from None


def _alert_message(condition: Condition, *, repeat: bool = False) -> str:
    """Pesan bebas credential — tidak menyertakan URL webhook atau DSN."""
    prefix = "ALERT BERULANG" if repeat else "ALERT"
    label = "gagal beruntun" if condition.alert_type == ALERT_FAILURES else "forecast basi"
    return (
        f"{prefix} forecast · site={condition.site_id} · {label}: "
        f"{condition.detail}. Periksa log job di GitHub Actions."
    )


def _recovery_message(site_id: str, alert_type: str) -> str:
    label = "gagal beruntun" if alert_type == ALERT_FAILURES else "forecast basi"
    return f"PEMULIHAN forecast · site={site_id} · kondisi '{label}' kembali normal."


def _parse_config(env: dict[str, str] | None = None) -> MonitorConfig:
    values = env if env is not None else os.environ
    try:
        return MonitorConfig(
            failure_streak=int(values.get("FORECAST_ALERT_FAILURE_STREAK", "3")),
            stale_hours=float(values.get("FORECAST_ALERT_STALE_HOURS", "3")),
            repeat_hours=float(values.get("FORECAST_ALERT_REPEAT_HOURS", "6")),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"konfigurasi FORECAST_ALERT_* tidak valid: {exc}") from exc


def _run_monitor(
    store,
    *,
    sites: Sequence[str],
    now: datetime,
    config: MonitorConfig,
    webhook_url: str,
    runs_to_read: int = DEFAULT_RUNS_TO_READ,
) -> int:
    """Evaluasi semua site & transisikan alert state. Return 0 sehat/kirim sukses.

    State tidak pernah ditandai terkirim sebelum webhook sukses. Bila kondisi
    buruk tapi URL kosong, exit nonzero — monitor sendiri tak boleh "hijau".
    """
    if not getattr(store, "_run_table_ok", False):
        raise RuntimeError(
            "tabel forecast_run belum ada — jalankan migration "
            "db/migrations/002_forecast_run.sql"
        )
    if not getattr(store, "_alert_state_table_ok", False):
        raise RuntimeError(
            "tabel forecast_alert_state belum ada — jalankan migration "
            "db/migrations/003_forecast_alert_state.sql"
        )

    failures: list[str] = []
    for site_id in sites:
        # Baris state memberi grace yang sama sejak monitor pertama melihat site.
        for alert_type in ALERT_TYPES:
            state = store.alert_state_get(site_id, alert_type)
            if state is None:
                store.alert_state_ensure(site_id, alert_type, now)
                state = store.alert_state_get(site_id, alert_type)
                if state is None:
                    raise RuntimeError("gagal menginisialisasi state alert")

        runs = store.run_status_rows(site_id, limit=runs_to_read)
        last_ok = store.last_successful_run_at(site_id)
        last_run = store.last_run_at(site_id)
        # State `first_seen_at` sudah ada untuk stale type.
        stale_state = store.alert_state_get(site_id, ALERT_STALE)
        assert stale_state is not None
        conditions = evaluate_site(
            site_id,
            runs,
            last_success_at=last_ok,
            last_run_at=last_run,
            first_seen_at=stale_state["first_seen_at"],
            now=now,
            config=config,
        )

        for alert_type in ALERT_TYPES:
            state = store.alert_state_get(site_id, alert_type)
            assert state is not None
            condition = conditions.get(alert_type)
            active = bool(state["active"])
            last_notified = _aware(state["last_notified_at"])

            if condition is not None:
                cooldown_before = now - timedelta(hours=config.repeat_hours)
                should_send = (
                    not active
                    or last_notified is None
                    or last_notified <= cooldown_before
                )
                if should_send:
                    if not webhook_url:
                        # Jalur fallback: GitHub Actions mengirim email saat job
                        # exit nonzero. Tandai siklus ini sebagai notifikasi via
                        # Actions agar polling 15 menit tidak mengirim email
                        # berulang; repeat berikutnya hanya sesudah cooldown.
                        store.alert_state_mark_notified(
                            site_id,
                            alert_type,
                            now=now,
                            cooldown_before=cooldown_before,
                        )
                        failures.append(
                            f"{site_id}/{alert_type}: {condition.detail}; "
                            "FORECAST_ALERT_WEBHOOK_URL tidak disetel — "
                            "GitHub Actions akan memberi tahu via email"
                        )
                        continue
                    repeat = active
                    send_webhook(webhook_url, _alert_message(condition, repeat=repeat))
                    marked = store.alert_state_mark_notified(
                        site_id,
                        alert_type,
                        now=now,
                        cooldown_before=cooldown_before,
                    )
                    if not marked:
                        # Pemeriksaan lain menandai terkirim setelah request
                        # webhook ini dimulai; log informational, jangan gagal.
                        log.info("alert sudah ditandai proses monitor lain: %s/%s",
                                 site_id, alert_type)
            elif active:
                if not webhook_url:
                    # Tanpa webhook, pemulihan dilaporkan lewat email
                    # Actions (job exit nonzero). Tandai state pulih agar
                    # polling berikutnya tidak mengirim email berulang.
                    store.alert_state_mark_recovered(site_id, alert_type, now=now)
                    failures.append(
                        f"{site_id}/{alert_type}: pemulihan — "
                        "FORECAST_ALERT_WEBHOOK_URL tidak disetel, "
                        "GitHub Actions akan memberi tahu via email"
                    )
                    continue
                send_webhook(webhook_url, _recovery_message(site_id, alert_type))
                store.alert_state_mark_recovered(site_id, alert_type, now=now)

    if failures:
        raise RuntimeError("; ".join(failures))
    return 0


def _main() -> int:
    """CLI monitor one-shot: ``python -m app.monitoring`` (GitHub Actions)."""
    from app.db import DatabaseUnavailable, PostgresForecastStore

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL tidak disetel — monitor run-log butuh penyedia DB.")
        return 1
    try:
        config = _parse_config()
    except ValueError as exc:
        print(str(exc))
        return 1

    sites = [
        site.strip()
        for site in os.environ.get("SITE_IDS", "PLTS_KUPANG").split(",")
        if site.strip()
    ]
    if not sites:
        print("SITE_IDS kosong — monitor tidak punya site untuk diperiksa.")
        return 1

    webhook_url = os.environ.get("FORECAST_ALERT_WEBHOOK_URL", "").strip()
    store = None
    try:
        store = PostgresForecastStore(dsn)
        _run_monitor(
            store,
            sites=sites,
            now=datetime.now(timezone.utc),
            config=config,
            webhook_url=webhook_url,
        )
    except (DatabaseUnavailable, RuntimeError, ValueError) as exc:
        # Exception httpx/psycopg bisa memuat URL/DSN (credential) di pesan.
        # Sensor HANYA baris yang memuat "://" (URL nyata) — nama env var
        # seperti FORECAST_ALERT_WEBHOOK_URL tetap dicetak karena ia
        # diperlukan untuk mendiagnosis, dan bukan rahasia.
        message = str(exc)
        if "://" in message:
            message = "monitor gagal memproses alert (credential tidak dicetak)"
        print(f"ERROR: {message}")
        return 1
    except Exception:
        log.exception("monitor gagal; detail credential disensor")
        print("ERROR: monitor gagal (detail dirahasiakan)")
        return 1
    finally:
        if store is not None:
            store.close()
    print(f"monitor forecast sehat: site={','.join(sites)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
