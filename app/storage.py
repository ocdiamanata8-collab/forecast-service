"""Penyimpanan forecast — kontrak data + penyedia bawaan di memori.

Sumber kebenaran penataan data: ``../KONTRAK_API_v1.md`` §5.3 (pagination,
urutan, dedupe) dan §8 (sumber data per endpoint).

Antarmuka sengaja dipisah dari penyimpanan:

- :class:`ForecastStore` — abstraksi baca/tulis; dipakai API.
- :class:`InMemoryForecastStore` — implementasi bawaan, tanpa dependensi,
  cukup untuk PoC.

Fase berikutnya tinggal menambah penyedia lain (SQLite/PostgreSQL) yang
memenuhi `ForecastStore` **tanpa mengubah endpoint**:

    class SqliteForecastStore(ForecastStore): ...

Aturan yang wajib dipertahankan implementasi mana pun (§5.3):

1. Urutan ``generated_at`` descending, tie-break ``forecast_id`` ascending.
2. Cursor opaque (base64); client tidak boleh membangun sendiri.
3. Filter (site_id/from/to) identik antar halaman; berubah di tengah ->
   ``E_CURSOR_FILTER_MISMATCH``.
4. Cursor rusak/kedaluwarsa -> ``E_CURSOR_INVALID``.
"""

from __future__ import annotations

import base64
import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4

from app.schemas import ForecastPoint


@dataclass(frozen=True)
class StoredForecast:
    """Satu rangkaian forecast yang sudah tersimpan."""

    forecast_id: str
    site_id: str
    target: str
    resolution: str
    unit: str
    ts: datetime
    generated_at: datetime
    horizon: int
    model_version: str
    forecast: tuple[ForecastPoint, ...]

    def __post_init__(self) -> None:
        for name, value in (("ts", self.ts), ("generated_at", self.generated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be tz-aware — naive timestamps forbidden")
        if len(self.forecast) != self.horizon:
            raise ValueError("len(forecast) must equal horizon")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must carry an explicit offset")
    return value.astimezone(timezone.utc)


def encode_cursor(generated_at: datetime, forecast_id: str) -> str:
    """Buat cursor opaque. Klien tidak boleh mengurai/membentuk sendiri."""
    payload = {"g": _utc(generated_at).isoformat(), "id": forecast_id}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Buka cursor. Raise ``ValueError`` bila rusak/kedaluwarsa/bukan milik kita."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        generated_at = datetime.fromisoformat(raw["g"])
        forecast_id = str(raw["id"])
    except Exception as exc:  # rusak / bukan base64 / struktur salah
        raise ValueError(
            "cursor is malformed or was not issued by this service; "
            "restart pagination from the first page"
        ) from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError(
            "cursor does not contain a timezone-aware timestamp; "
            "restart pagination from the first page"
        )
    return generated_at, forecast_id


class ForecastRunBusy(RuntimeError):
    """Pemicu lain sedang memproses site yang sama — run ini dilewati.

    Dilempar oleh penyedia yang mendukung anti-tumpang-tindih (lihat
    ``PostgresForecastStore.site_run_lock``) bila kunci advisory per-site
    sudah dipegang proses lain. Sengaja DILEWATI, bukan diantre: antrian
    tetap menghasilkan batch kedua (sama saja dengan menulis forecast
    duplikat setelah job pertama selesai). Pemicu kedua memperoleh
    kondisi "sudah berjalan" dan berhenti tanpa menyentuh store.

    Didefinisikan di sini (bukan di ``app/db.py``) supaya scheduler dan
    endpoint job bisa menangkapnya tanpa mengimpor penyedia DB — lapisan
    tetap bersih (lihat README "Pemisahan lapisan").
    """


class ForecastStore(ABC):
    """Antarmuka simpan/ambil forecast."""

    @abstractmethod
    def save(self, item: StoredForecast) -> None:
        """Simpan satu forecast. Idempoten pada ``forecast_id`` yang sama."""

    @abstractmethod
    def latest(self, site_id: str) -> StoredForecast | None:
        """Forecast terbaru untuk site (generated_at terbesar)."""

    @abstractmethod
    def history(
        self,
        *,
        site_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[StoredForecast], str | None]:
        """Riwayat terurut ``generated_at`` descending.

        Returns
        -------
        ``(items, next_cursor)`` — ``next_cursor`` ``None`` bila ``has_more`` False.
        Raise ``ValueError`` pada cursor rusak atau filter berubah di tengah.
        """

    def count(self) -> int:  # pragma: no cover - penyedia lain boleh override
        return 0


class InMemoryForecastStore(ForecastStore):
    """Penyedia bawaan: di memori, deterministik, tanpa dependensi eksternal.

    Kunci internal memakai ``generated_at`` UTC agar perbandingan lintas zona
    sama (kontrak §1.5).
    """

    def __init__(self) -> None:
        self._items: dict[str, StoredForecast] = {}
        self._lock = threading.Lock()

    def save(self, item: StoredForecast) -> None:
        with self._lock:
            self._items[item.forecast_id] = item

    def latest(self, site_id: str) -> StoredForecast | None:
        with self._lock:
            candidates = [i for i in self._items.values() if i.site_id == site_id]
        if not candidates:
            return None
        return max(candidates, key=lambda i: (_utc(i.generated_at), i.forecast_id))

    def history(
        self,
        *,
        site_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[StoredForecast], str | None]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        if cursor is not None:
            cursor_gen, cursor_id = decode_cursor(cursor)
        else:
            cursor_gen, cursor_id = None, None

        with self._lock:
            pool = [i for i in self._items.values() if i.site_id == site_id]

        if start is not None:
            start_utc = _utc(start)
            pool = [i for i in pool if _utc(i.generated_at) >= start_utc]
        if end is not None:
            end_utc = _utc(end)
            pool = [i for i in pool if _utc(i.generated_at) <= end_utc]

        # Kontrak §5.3-3: urut generated_at desc, tie-break forecast_id asc.
        pool.sort(key=lambda i: (_utc(i.generated_at), i.forecast_id), reverse=True)
        pool.sort(key=lambda i: i.forecast_id)  # tie-break stabil asc
        pool.sort(key=lambda i: _utc(i.generated_at), reverse=True)

        if cursor_gen is not None:
            def strictly_after(item: StoredForecast) -> bool:
                gen = _utc(item.generated_at)
                if gen < cursor_gen:
                    return True
                return gen == cursor_gen and item.forecast_id > cursor_id
            pool = [i for i in pool if strictly_after(i)]

        page = pool[:limit]
        has_more = len(pool) > limit
        next_cursor = (
            encode_cursor(page[-1].generated_at, page[-1].forecast_id)
            if has_more and page
            else None
        )
        return page, next_cursor

    def count(self) -> int:
        with self._lock:
            return len(self._items)


def items_from_response(
    forecast_id: str,
    site_id: str,
    target: str,
    resolution: str,
    unit: str,
    ts: datetime,
    generated_at: datetime,
    model_version: str,
    points: Iterable[ForecastPoint],
) -> StoredForecast:
    """Bangun :class:`StoredForecast` dari objek response Pydantic."""
    pts = tuple(points)
    return StoredForecast(
        forecast_id=forecast_id,
        site_id=site_id,
        target=target,
        resolution=resolution,
        unit=unit,
        ts=ts,
        generated_at=generated_at,
        horizon=len(pts),
        model_version=model_version,
        forecast=pts,
    )


def new_forecast_id() -> str:
    """UUID untuk forecast — deterministik TIDAK diperlukan di sini (bukan model)."""
    return str(uuid4())
