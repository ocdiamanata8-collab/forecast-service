"""Penyimpanan forecast di PostgreSQL — memenuhi kontrak :class:`ForecastStore`.

Fase 6 · item 6·02. Endpoint ``latest``/``history`` TIDAK berubah sama sekali:
keduanya sudah membaca ``request.app.state.store`` (antarmuka store dipisah dari
sejak Fase 1 — lihat docstring :mod:`app.storage`); penyedia ini cukup
menggantikan :class:`InMemoryForecastStore` di lifespan bila ``DATABASE_URL``
di-set.

Aturan yang dipertahankan (§5.3 + migration ``001_forecast_and_actual.sql``):

1. **Write-once, tidak pernah menimpa.** INSERT dengan
   ``ON CONFLICT (forecast_id, forecast_time) DO NOTHING`` — idempoten per
   ``forecast_id`` (setara ``InMemoryForecastStore.save``), dan lintas batch
   dijaga ``UNIQUE (site_id, generated_at, forecast_time)``.
2. **Urutan ``generated_at`` descending, tie-break ``forecast_id`` ascending**
   — identik dengan penyedia in-memory.
3. Cursor opaque dari :func:`storage.encode_cursor` / :func:`storage.decode_cursor`
   — klien tidak pernah mengurai/membentuk sendiri; filter berubah di tengah
   paginasi → ``ValueError`` (dipetakan endpoint ke ``E_CURSOR_FILTER_MISMATCH``).
4. Semua timestamp tz-aware. ``timestamptz`` dibaca psycopg sebagai datetime
   aware; zonanya = offset yang disimpan saat tulis (+08:00) — persis yang
   dipastikan test kontrak (``endswith("+08:00")``).

Dua operasi baca selalu dua langkah kecil (keduanya index-backed):

* **batch list** — ``forecast_id`` + ``generated_at`` per batch (``LIMIT`` per
  halaman, ``limit + 1`` untuk mengetahui ada halaman berikut);
* **points** — seluruh baris titik untuk batch-batch terpilih, terurut
  ``horizon_step``.

Yang TIDAK dipindah ke sini: ``count()`` bawaan tetap 0 (tak ada pemanggil di
aplikasi — hanya dipakai test in-memory), dan tabel ``actual_result`` diisi
oleh backfill 6·04, bukan store ini.

DSN disimpan di environment ``DATABASE_URL`` (§6·05) — tidak pernah di kode.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from app.schemas import ForecastPoint, SITE_TZ
from app.storage import (
    ForecastStore,
    StoredForecast,
    decode_cursor,
    encode_cursor,
)

log = logging.getLogger(__name__)

# ── query tulis ──────────────────────────────────────────────────────────
# Satu baris per titik per batch; idempoten pada (forecast_id, forecast_time).
# horizon_step 1-based (§8) — dipakai akurasi per-horizon (8·07).
_INSERT_SQL = """
    INSERT INTO forecast_result (
        forecast_id, site_id, target, resolution, unit,
        ts, forecast_time, generated_at,
        forecast_value, horizon_step, model_version
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (forecast_id, forecast_time) DO NOTHING
"""

# ── query baca ───────────────────────────────────────────────────────────
# Klausa filter bangun DI SINI (bukan SQL statis dengan parameter NULL):
# psycopg merender parameter None tanpa tipe → Postgres menolak dengan
# "could not determine data type of parameter" (diajarkan uji live 6·02).
# Parameter tetap selalu dibinding — nilai tidak pernah disisipkan teks.
_BATCH_SELECT = """
    SELECT DISTINCT forecast_id, generated_at
      FROM forecast_result
"""


def _batch_query(
    *,
    site_id: str,
    start: datetime | None,
    end: datetime | None,
    after: datetime | None,
    after_id: str | None,
    limit: int,
) -> tuple[str, dict[str, object]]:
    """Susun query batch + parameter. Filter ada → klausa ikut (§5.3)."""
    where = ["site_id = %(site_id)s"]
    params: dict[str, object] = {"site_id": site_id, "limit": limit}
    if start is not None:
        where.append("generated_at >= %(start)s")
        params["start"] = start
    if end is not None:
        where.append("generated_at <= %(end)s")
        params["end"] = end
    if after is not None and after_id is not None:
        # tie-break persis kontrak §5.3-3: lewat generated_at, lalu forecast_id.
        where.append(
            "(generated_at < %(after)s"
            " OR (generated_at = %(after)s AND forecast_id > %(after_id)s))"
        )
        params["after"] = after
        params["after_id"] = after_id
    sql = (
        _BATCH_SELECT
        + " WHERE " + " AND ".join(where)
        + " ORDER BY generated_at DESC, forecast_id ASC"
        + " LIMIT %(limit)s"
    )
    return sql, params

# Seluruh titik milik batch-batch terpilih — meta ikut tiap baris.
# Kolom per-batch adalah ``horizon_step`` (6·01: rename dari ``horizon``);
# horizon TOTAL per batch = jumlah titiknya — divalidasi StoredForecast.
_POINTS_SQL = """
    SELECT forecast_id, site_id, target, resolution, unit, ts,
           generated_at, model_version,
           forecast_time, forecast_value, horizon_step
      FROM forecast_result
     WHERE forecast_id = ANY(%(ids)s)
     ORDER BY generated_at DESC, forecast_id ASC, horizon_step ASC
"""

_PING_SQL = "SELECT 1"
_HAS_TABLE_SQL = "SELECT to_regclass('forecast_result')"
_HAS_RUN_TABLE_SQL = "SELECT to_regclass('forecast_run')"

# ── run log (6·03 / 7·04) ────────────────────────────────────────────────
# Satu baris per run scheduler per site. JANGAN dibatasi constraint FK ke
# forecast_result: run GAGAL tidak punya batch sama sekali — justru baris
# itulah yang mendeteksi "kegagalan diam-diam".
_INSERT_RUN_OK_SQL = """
    INSERT INTO forecast_run (
        site_id, started_at, finished_at, status,
        rows_saved, forecast_id, generated_at, model_version
    ) VALUES (%s, %s, %s, 'ok', %s, %s, %s, %s)
"""
_INSERT_RUN_FAILED_SQL = """
    INSERT INTO forecast_run (
        site_id, started_at, finished_at, status, rows_saved, error
    ) VALUES (%s, %s, %s, 'failed', 0, %s)
"""

# ── backfill aktual (6·04) ───────────────────────────────────────────────
# Sinkron deret aktual → actual_result (upsert per pengamatan: koreksi
# re-ingest = data sama, bukan menimpa riwayat forecast).
_SYNC_ACTUALS_SQL = """
    INSERT INTO actual_result (site_id, ts, actual_value, unit, source)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (site_id, ts) DO UPDATE
       SET actual_value = EXCLUDED.actual_value,
           unit         = EXCLUDED.unit,
           source       = EXCLUDED.source,
           observed_at  = now()
"""
# Isi baris forecast yang waktunya sudah lewat — HANYA yang masih NULL.
_FILL_PENDING_SQL = """
    UPDATE forecast_result f
       SET actual_value = a.actual_value
      FROM actual_result a
     WHERE f.site_id = a.site_id
       AND f.forecast_time = a.ts
       AND f.actual_value IS NULL
       AND f.forecast_time < %(cutoff)s::timestamptz
"""

# ── retensi (6·06) ───────────────────────────────────────────────────────
# HAPUS batch yang generated_at-nya lebih tua dari N hari. Prinsip checklist:
# default = JANGAN DIHAPUS (bukti akurasi; volume kecil). Sengaja DELETE per
# forecast_id UTUH (bukan per baris) agar riwayat run tidak pernah terpotong
# di tengah batch.
_DELETE_OLD_BATCH_SQL = """
    DELETE FROM forecast_result
     WHERE forecast_id IN (
       SELECT DISTINCT forecast_id
         FROM forecast_result
        WHERE generated_at < %(cutoff)s::timestamptz
     )
"""
_DELETE_OLD_RUN_SQL = """
    DELETE FROM forecast_run WHERE started_at < %(cutoff)s::timestamptz
"""
_DELETE_OLD_ACTUAL_SQL = """
    DELETE FROM actual_result WHERE ts < %(cutoff)s::timestamptz
"""


class DatabaseUnavailable(RuntimeError):
    """``DATABASE_URL`` terkonfigurasi tetapi database tidak dapat dipakai.

    Sengaja GAGAL FAST saat startup — bukan fallback diam-diam ke in-memory:
    berjalan tanpa persistensi diam-diam = kebalikan dari tujuan Fase 6
    (forecast lenyap tiap restart sementara "latest dari database" bohong).
    """


def _forecast_id(item: StoredForecast) -> str:
    return item.forecast_id


def _group_points(rows: list[tuple]) -> list[StoredForecast]:
    """Kelompokkan baris terurut menjadi :class:`StoredForecast` per batch.

    Rows datang ``ORDER BY generated_at DESC, forecast_id ASC, horizon_step
    ASC`` — urutan masuk sudah urutan batch maupun urutan titik. Meta batch
    dikumpulkan dulu, baru :class:`StoredForecast` dibangun SEKALI dengan
    titik lengkap (konstruktor menolak ``len(forecast) != horizon``).
    """
    meta: dict[str, dict] = {}
    order: list[str] = []
    points: dict[str, list[ForecastPoint]] = {}
    for (
        raw_fid, site_id, target, resolution, unit, ts,
        generated_at, model_version,
        forecast_time, forecast_value, horizon_step,
    ) in rows:
        # Kolom uuid → str: kontrak §3.2 memakai string, dan cursor §5.3
        # di-JSON (objek UUID tidak serializable).
        forecast_id = str(raw_fid)
        # timestamptz selalu dibaca UTC (perilaku psycopg); store in-memory
        # mengembalikan apa adanya (WITA). Normalisasi ke zona site di sini
        # supaya kedua penyedia SETARA dan keluaran response §1.5 tetap +08:00
        # walau serialisasi Pydantic juga menormalisasi (garis ganda, bukan
        # tempat berbeda).
        ts = ts.astimezone(SITE_TZ)
        generated_at = generated_at.astimezone(SITE_TZ)
        if forecast_id not in meta:
            meta[forecast_id] = {
                "forecast_id": forecast_id,
                "site_id": site_id,
                "target": target,
                "resolution": resolution,
                "unit": unit,
                "ts": ts,
                "generated_at": generated_at,
                "model_version": model_version,
            }
            order.append(forecast_id)
            points[forecast_id] = []
        points[forecast_id].append(
            ForecastPoint(
                forecast_time=forecast_time.astimezone(SITE_TZ),
                forecast_value=float(forecast_value),
                model_version=model_version,
            )
        )
    return [
        # horizon total = jumlah titik batch (setara kontrak: len==horizon)
        StoredForecast(**{**meta[fid], "horizon": len(points[fid])},
                       forecast=tuple(points[fid]))
        for fid in order
    ]


class PostgresForecastStore(ForecastStore):
    """Penyedia PostgreSQL lewat connection pool psycopg v3.

    Dibuka sekali saat lifespan; ``close()`` dipanggil saat shutdown.

    6·05 — pemisahan baca/tulis: ``read_dsn`` (env ``DATABASE_READONLY_URL``)
    memakai koneksi TERPISAH dengan hak baca saja bila platform/database
    mendukungnya — cukup untuk endpoint ``latest``/``history`` yang dipakai
    dashboard. Tanpa ``read_dsn``, kedua operasi memakai pool yang sama
    (satu DSN ``DATABASE_URL`` = tulis + baca). Prinsip least-privilege:
    kredensial baca yang bocor tidak bisa menulis/menimpa riwayat forecast.
    """

    def __init__(
        self,
        dsn: str,
        *,
        read_dsn: str | None = None,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        def make_pool(conninfo: str) -> ConnectionPool:
            return ConnectionPool(
                conninfo=conninfo,
                min_size=min_size,
                max_size=max_size,
                # Fail fast: koneksi pertama wajib sukses di sini (startup),
                # bukan menunggu request pertama dari platform monitoring.
                open=False,
                kwargs={"row_factory": tuple_row, "autocommit": True},
            )

        self._write_pool = make_pool(dsn)
        # read_dsn kosong / sama dengan dsn → satu pool dipakai bersama
        # (tanpa membuang koneksi dobel).
        if read_dsn and read_dsn != dsn:
            self._read_pool = make_pool(read_dsn)
        else:
            self._read_pool = self._write_pool
        # Satu lock tulis: urutan batch per tick deterministik di log/DB.
        self._write_lock = threading.Lock()
        self._verify()

    # ── startup ────────────────────────────────────────────────────────
    def _verify(self) -> None:
        """Ping + pastikan tabel migration 6·01 sudah ada.

        Tabel ``forecast_run`` (migration 002/6·03) juga dicek: bila belum
        ada, store TETAP dipakai (batch forecast tetap jalan) dan pencatatan
        run dilewati dengan peringatan — agar migrasi 001→002 boleh
        bertahap tanpa menjatuhkan service.
        """
        try:
            # Pool BACA diverifikasi lebih dulu: kredensial baca (6·05) yang
            # salah menolak startup sebelum scheduler sempat menulis.
            for pool in dict.fromkeys((self._read_pool, self._write_pool)):
                pool.open(wait=True, timeout=10)
                with pool.connection() as conn:
                    conn.execute(_PING_SQL)
            with self._write_pool.connection() as conn:
                row = conn.execute(_HAS_TABLE_SQL).fetchone()
                run_row = conn.execute(_HAS_RUN_TABLE_SQL).fetchone()
        except Exception as exc:
            self.close()
            raise DatabaseUnavailable(
                f"DATABASE_URL tidak dapat dipakai: {exc}. "
                "Cek DSN (kontrak §6.05) dan pastikan migration "
                "db/migrations/001_forecast_and_actual.sql sudah dijalankan."
            ) from exc
        if row is None or row[0] is None:
            self.close()
            raise DatabaseUnavailable(
                "tabel forecast_result belum ada — jalankan dulu "
                "db/migrations/001_forecast_and_actual.sql (Fase 6 · 6·01)"
            )
        self._run_table_ok = run_row is not None and run_row[0] is not None
        if not self._run_table_ok:
            log.warning(
                "store: tabel forecast_run belum ada — run log nonaktif "
                "(jalankan db/migrations/002_forecast_run.sql untuk 6·03)"
            )
        log.info("store: PostgreSQL aktif (forecast_result terverifikasi)")

    @property
    def _pool(self) -> ConnectionPool:
        """Alias untuk pool TULIS — dipakai test/observability (mis. pembersihan
        baris uji lewat DELETE = operasi tulis, harus lewat kredensial tulis)."""
        return self._write_pool

    def close(self) -> None:
        """Tutup pool baca & tulis (satu bila berbagi)."""
        self._read_pool.close()
        if self._write_pool is not self._read_pool:
            self._write_pool.close()

    # ── ForecastStore ──────────────────────────────────────────────────
    def save(self, item: StoredForecast) -> None:
        """Satu batch = ``horizon`` baris dalam satu transaksi, idempoten."""
        rows = [
            (
                item.forecast_id, item.site_id, item.target,
                item.resolution, item.unit,
                item.ts, p.forecast_time, item.generated_at,
                p.forecast_value, step, item.model_version,
            )
            for step, p in enumerate(item.forecast, start=1)
        ]
        with self._write_lock, self._write_pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(_INSERT_SQL, rows)
        log.info(
            "store: menyimpan site=%s id=%s baris=%s generated_at=%s",
            item.site_id, item.forecast_id, len(rows),
            item.generated_at.isoformat(),
        )

    def latest(self, site_id: str) -> StoredForecast | None:
        """Batch terbaru untuk site — inilah yang dibaca ``GET /forecast/latest``.

        6·02: satu batch terpilih via index, seluruh titiknya diambil sekaligus;
        endpoint hanya memetakan ke response schema (tanpa komputasi forecast).
        """
        with self._read_pool.connection() as conn:
            sql, params = _batch_query(
                site_id=site_id, start=None, end=None,
                after=None, after_id=None, limit=1,
            )
            batches = conn.execute(sql, params).fetchall()
            if not batches:
                return None
            rows = conn.execute(_POINTS_SQL, {"ids": [batches[0][0]]}).fetchall()
        items = _group_points(rows)
        return items[0] if items else None

    def history(
        self,
        *,
        site_id: str,
        start=None,
        end=None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[StoredForecast], str | None]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        cursor_gen, cursor_id = (None, None)
        if cursor is not None:
            cursor_gen, cursor_id = decode_cursor(cursor)

        sql, params = _batch_query(
            site_id=site_id,
            start=start,
            end=end,
            after=cursor_gen,
            after_id=cursor_id,
            # limit + 1 → tahu ada halaman berikut tanpa query ketiga
            limit=limit + 1,
        )
        with self._read_pool.connection() as conn:
            batches = conn.execute(sql, params).fetchall()
            has_more = len(batches) > limit
            batches = batches[:limit]
            if not batches:
                return [], None
            rows = conn.execute(
                _POINTS_SQL, {"ids": [b[0] for b in batches]}
            ).fetchall()

        items = _group_points(rows)
        next_cursor = (
            encode_cursor(items[-1].generated_at, items[-1].forecast_id)
            if has_more and items
            else None
        )
        return items, next_cursor

    def count(self) -> int:
        """Jumlah baris titik di DB — untuk observability (bukan test PoC)."""
        with self._read_pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM forecast_result").fetchone()
        return int(row[0]) if row else 0

    # ── retensi (6·06 — opsional di interface) ─────────────────────────────
    def prune_forecasts(
        self,
        *,
        older_than_days: int,
        prune_actuals: bool = True,
    ) -> dict[str, int]:
        """Hapus forecast lama (batch UTUH) + run log lama.

        Panggil HANYA bila ``FORECAST_RETENTION_DAYS > 0`` — default 0 =
        tidak pernah dihapus (bukti akurasi model; volume kecil). Batch
        dihapus utuh supaya riwayat run tidak terpotong di tengah.

        ``prune_actuals=False`` mempertahankan deret pengamatan — aktual
        berlangganan lama berguna sebagai pembanding lintas-model walau
        forecast lama sudah dibuang.
        """
        if older_than_days <= 0:
            return {"rows": 0, "runs": 0, "actuals": 0}
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=older_than_days)
        with self._write_pool.connection() as conn:
            with conn.transaction():
                before = conn.execute(
                    "SELECT count(*) FROM forecast_result"
                ).fetchone()[0]
                conn.execute(_DELETE_OLD_BATCH_SQL, {"cutoff": cutoff})
                after = conn.execute(
                    "SELECT count(*) FROM forecast_result"
                ).fetchone()[0]
                run_cur = conn.execute(_DELETE_OLD_RUN_SQL, {"cutoff": cutoff})
                runs = run_cur.rowcount if run_cur.rowcount and run_cur.rowcount > 0 else 0
                actuals = 0
                if prune_actuals:
                    act_cur = conn.execute(_DELETE_OLD_ACTUAL_SQL, {"cutoff": cutoff})
                    actuals = act_cur.rowcount if act_cur.rowcount and act_cur.rowcount > 0 else 0
        summary = {"rows": int(before - after), "runs": int(runs),
                   "actuals": int(actuals)}
        log.info(
            "retensi: >%s hari → %s baris forecast, %s run, %s aktual terhapus "
            "(cutoff=%s)",
            older_than_days, summary["rows"], summary["runs"],
            summary["actuals"], cutoff.isoformat(),
        )
        return summary

    # ── run log (6·03 — opsional di interface; in-memory tidak mencatat) ──
    def record_run(
        self,
        *,
        site_id: str,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        rows_saved: int = 0,
        forecast_id: str | None = None,
        generated_at: datetime | None = None,
        model_version: str | None = None,
        error: str | None = None,
    ) -> None:
        """Catat satu run scheduler — sukses ATAU gagal.

        Gagal tetap ditulis (dengan ``error`` terisi) supaya kegagalan tidak
        pernah diam-diam. Bila tabel ``forecast_run`` belum terpasang
        (migration 002 belum dijalankan), pencatatan dilewati dengan log
        peringatan — run forecast sendiri TIDAK ikut gagal karena itu.
        """
        if not self._run_table_ok:
            log.warning(
                "run log: tabel forecast_run belum ada — jalankan "
                "db/migrations/002_forecast_run.sql (6·03). site=%s status=%s",
                site_id, status,
            )
            return
        try:
            if status == "ok":
                args = (
                    site_id, started_at, finished_at, int(rows_saved),
                    forecast_id, generated_at, model_version,
                )
                sql = _INSERT_RUN_OK_SQL
            else:
                args = (site_id, started_at, finished_at, (error or "unknown")[:500])
                sql = _INSERT_RUN_FAILED_SQL
            with self._write_pool.connection() as conn:
                conn.execute(sql, args)
        except Exception:  # pencatatan run tidak boleh menjatuhkan tick
            log.exception("run log: gagal mencatat run site=%s status=%s",
                          site_id, status)

    # ── backfill aktual (6·04 — opsional di interface) ─────────────────────
    def sync_actuals(self, rows) -> int:
        """Upsert deret aktual ke ``actual_result``. Mengembalikan jumlah baris."""
        rows = list(rows)
        if not rows:
            return 0
        with self._write_pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(_SYNC_ACTUALS_SQL, rows)
        return len(rows)

    def fill_pending_actuals(self, *, cutoff: datetime) -> int:
        """Isi ``forecast_result.actual_value`` dari ``actual_result`` untuk
        baris yang ``forecast_time < cutoff`` dan masih NULL.

        Mengembalikan jumlah baris yang terisi. HANYA mengisi NULL — baris
        yang sudah terisi tidak pernah diubah (6·04: kolom diisi menyusul).
        """
        with self._write_pool.connection() as conn:
            cur = conn.execute(_FILL_PENDING_SQL, {"cutoff": cutoff})
            filled = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        return int(filled)
