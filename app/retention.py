"""Retensi data forecast — Fase 6 · item 6·06.

Kebijakan default (mengikuti checklist master): **retensi 0 = simpan selamanya.**
Alasannya tertulis di checklist: *data forecast historis yang dihapus akan
menghapus bukti akurasi model — biasanya tidak perlu dihapus sama sekali
karena volumenya kecil* (per batch harian ≈ 7 baris × horizon).

Retensi HANYA aktif bila di-set eksplisit:

    FORECAST_RETENTION_DAYS=365      # hapus batch > 365 hari (opsional)
    FORECAST_RETENTION_PRUNE_ACTUALS=false   # pertahankan deret aktual (default)
    RETENTION_INTERVAL_SECONDS=86400 # interval cek (default: 1× sehari)

Dampak keputusan:

- Batch dihapus UTUH per ``forecast_id`` — riwayat run tidak pernah
  terpotong di tengah (bukti 6·03 tetap utuh per run).
- Deret pengamatan (``actual_result``) TIDAK dihapus pada default — pembanding
  lintas-model lebih lama daripada forecast yang dibandingkan.
- Backup (``scripts/backup_db.py``) terpisah dan sebaiknya dijadwalkan cron
  berbeda — retensi mengurangi data online, backup menjaga kemampuan pulih.

Sama seperti backfill: tanpa ``DATABASE_URL`` (PoC in-memory) tidak ada yang
dipanggil — store tanpa ``prune_forecasts`` dilewati tanpa error.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

log = logging.getLogger("app.retention")

#: Default: JANGAN MENYIMPAN JADWAL HAPUS — 0 = tidak pernah dihapus.
DEFAULT_RETENTION_DAYS = 0

#: Cek per hari — retensi tidak perlu lebih sering dari itu.
DEFAULT_INTERVAL_SECONDS = 86400.0


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


async def retention_loop(
    store,
    *,
    days: int,
    prune_actuals: bool = True,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    immediate: bool = False,
    run: Callable[..., dict[str, int] | None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Loop retensi: (opsional segera) → tidur interval → prune → ...

    Kegagalan satu siklus dicatat dan dicoba lagi berikutnya — sama seperti
    loop backfill (7·04: kegagalan terlihat, tidak mematikan layanan).
    """
    do_run = run or (lambda: prune_once(store, days=days, prune_actuals=prune_actuals))
    do_sleep = sleep or asyncio.sleep

    async def once() -> None:
        try:
            await asyncio.to_thread(do_run)
        except Exception:
            log.exception("retensi gagal — mencoba lagi pada siklus berikutnya")

    if immediate:
        await once()
    while True:
        await do_sleep(interval)
        await once()
