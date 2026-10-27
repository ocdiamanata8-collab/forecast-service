"""Retensi data forecast — Fase 6 · item 6·06.

Kebijakan default (mengikuti checklist master): **retensi 0 = simpan selamanya.**
Alasannya tertulis di checklist: *data forecast historis yang dihapus akan
menghapus bukti akurasi model — biasanya tidak perlu dihapus sama sekali
karena volumenya kecil* (per batch harian ≈ 7 baris × horizon).

Retensi HANYA aktif bila di-set eksplisit:

    FORECAST_RETENTION_DAYS=365      # hapus batch > 365 hari (opsional)
    FORECAST_RETENTION_PRUNE_ACTUALS=false   # pertahankan deret aktual (default)

Jadwal penghapusan dijalankan **sekali sehari** oleh job scheduler eksternal;

- Batch dihapus UTUH per ``forecast_id`` — riwayat run tidak pernah
  terpotong di tengah (bukti 6·03 tetap utuh per run).
- Deret pengamatan (``actual_result``) TIDAK dihapus pada default — pembanding
  lintas-model lebih lama daripada forecast yang dibandingkan.
- Backup (``scripts/backup_db.py``) terpisah dan sebaiknya dijadwalkan cron
  berbeda — retensi mengurangi data online, backup menjaga kemampuan pulih.

Penjadwalan berulang dilakukan di luar proses web (GitHub Actions harian,
lihat ``.github/workflows/forecast-jobs.yml``; job otomatis dilewati bila
retensi belum aktif). Loop di dalam proses web dihapus: mati saat restart dan
tidak terlihat di log deploy. CLI one-shot:

    python -m app.retention

Sama seperti backfill: tanpa ``DATABASE_URL`` (PoC in-memory) tidak ada yang
dipanggil — store tanpa ``prune_forecasts`` dilewati tanpa error.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("app.retention")

#: Default: JANGAN MENYIMPAN JADWAL HAPUS — 0 = tidak pernah dihapus.
DEFAULT_RETENTION_DAYS = 0


def retention_days_from_env(env: dict[str, str] | None = None) -> int:
    """Baca ``FORECAST_RETENTION_DAYS`` (default 0 = tanpa hapus).

    Nilai negatif atau tidak valid → 0 (tanpa hapus) + peringatan —
    gagal aman, bukan gagal menghapus.
    """
    raw = (env if env is not None else os.environ).get(
        "FORECAST_RETENTION_DAYS", ""
    )
    try:
        days = int(raw) if raw.strip() else DEFAULT_RETENTION_DAYS
    except ValueError:
        log.warning("FORECAST_RETENTION_DAYS=%r tidak valid — retensi mati "
                    "(0 = tidak pernah dihapus)", raw)
        return DEFAULT_RETENTION_DAYS
    if days < 0:
        log.warning("FORECAST_RETENTION_DAYS=%s < 0 — retensi mati", days)
        return DEFAULT_RETENTION_DAYS
    return days


def prune_actuals_enabled(env: dict[str, str] | None = None) -> bool:
    """Deret aktual ikut dipangkas? Default TIDAK (bahan pembanding)."""
    raw = (env if env is not None else os.environ).get(
        "FORECAST_RETENTION_PRUNE_ACTUALS", ""
    ).strip().lower()
    return raw in {"1", "true", "yes"}


def prune_once(store, *, days: int, prune_actuals: bool) -> dict[str, int] | None:
    """Satu siklus retensi. Kembalikan ringkasan, atau ``None`` bila store
    tak mendukung (in-memory/PoC) atau retensi mati (days=0)."""
    method = getattr(store, "prune_forecasts", None)
    if not callable(method):
        return None
    if days <= 0:
        return None
    summary = method(older_than_days=days, prune_actuals=prune_actuals)
    log.info("retensi selesai: %s", summary)
    return summary


def _main() -> int:
    """CLI sekali-jalan: ``python -m app.retention``.

    Dipakai job GitHub Actions (jadwal harian) & pemanggilan manual. Tanpa
    ``DATABASE_URL`` → exit 1 (tidak ada tempat menghapus). Retensi default
    mati (``FORECAST_RETENTION_DAYS`` kosong/0) → exit 0 tanpa menghapus apa
    pun — menghapus adalah tindakan tak bisa dibatalkan, jadi tidak pernah
    terjadi diam-diam.
    """
    from app.db import PostgresForecastStore

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL tidak disetel — retensi butuh penyedia DB.")
        return 1
    days = retention_days_from_env()
    if days <= 0:
        print("retensi mati (FORECAST_RETENTION_DAYS kosong/0) — tidak ada "
              "yang dihapus (bukti akurasi, volume kecil)")
        return 0
    prune_actuals = prune_actuals_enabled()
    store = PostgresForecastStore(dsn)
    try:
        summary = prune_once(store, days=days, prune_actuals=prune_actuals)
    finally:
        store.close()
    if summary is None:
        print("store tanpa dukungan prune_forecasts — dilewati")
        return 0
    print(f"retensi selesai: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
