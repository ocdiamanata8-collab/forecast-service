-- ═══════════════════════════════════════════════════════════════════════
-- 002 — RUN LOG PENJADWALAN   ·   Fase 6 · item 6·03 (jejak run)
-- SEI Forecasting Service · PostgreSQL / Supabase
--
-- Riwayat RUN: satu baris per run scheduler per site — kapan mulai, kapan
-- selesai, sukses/gagal, berapa baris tersimpan, dan errornya. Tanpa tabel
-- ini kegagalan scheduler hanya ada di log proses yang hilang saat restart —
-- "run gagal diam-diam selama sepekan" persis yang dicegah checklist (7·04).
--
-- Catatan: batch forecast hasil run TIDAK disimpan di sini melainkan di
-- forecast_result (migration 001) — kolom forecast_id/generated_at di bawah
-- adalah PENUNJUK ke batch yang ditulis run tersebut.
--
-- Cara apply:
--   psql "$DATABASE_URL" -f db/migrations/002_forecast_run.sql
--   (atau Supabase SQL Editor → tempel → Run)
-- Idempotent: aman dijalankan berulang.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS forecast_run (
    id            bigserial PRIMARY KEY,
    site_id       text         NOT NULL,
    started_at    timestamptz  NOT NULL,          -- run dimulai
    finished_at   timestamptz  NOT NULL,          -- run selesai (ok/gagal)
    status        text         NOT NULL,          -- 'ok' | 'failed'
    rows_saved    int          NOT NULL DEFAULT 0,-- baris forecast ditulis
    forecast_id   uuid,                           -- batch yang ditulis (NULL bila gagal)
    generated_at  timestamptz,                    -- generated_at batch tsb
    model_version text,                           -- versi model saat run (NULL bila gagal)
    error         text,                           -- pesan kegagalan (NULL bila ok)
    CONSTRAINT ck_forecast_run_status CHECK (status IN ('ok', 'failed')),
    CONSTRAINT ck_forecast_run_rows   CHECK (rows_saved >= 0),
    CONSTRAINT ck_forecast_run_error
        CHECK ((status = 'ok' AND error IS NULL)
            OR (status = 'failed' AND error IS NOT NULL))
);

-- Pertanyaan operasional utama: run terakhir per site + rentang waktu
-- (mendeteksi kegagalan beruntun & jeda regenerasi melebihi SLA §5.2).
CREATE INDEX IF NOT EXISTS ix_forecast_run_site_started
    ON forecast_run (site_id, started_at DESC);

COMMENT ON TABLE forecast_run IS
    'Satu baris per run scheduler per site: started_at, finished_at, status, rows_saved, error (7·04). Gagal = error terisi, forecast_id NULL.';
COMMENT ON COLUMN forecast_run.status IS
    'ok bila batch tersimpan; failed bila run melempar galat (dicatat, run site berikut tetap jalan).';
COMMENT ON COLUMN forecast_run.forecast_id IS
    'Penunjuk ke batch di forecast_result yang ditulis run ini; NULL untuk run gagal.';
