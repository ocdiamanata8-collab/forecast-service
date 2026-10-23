-- ═══════════════════════════════════════════════════════════════════════
-- 001 — TABEL FORECAST & ACTUAL   ·   Fase 6 · item 6·01
-- SEI Forecasting Service · PostgreSQL / Supabase
--
-- SATU BARIS PER FORECAST PER TIMESTAMP — TIDAK PERNAH MENIMPA.
-- Setiap run scheduler menulis batch baru (generated_at sendiri); baris lama
-- disimpan sebagai bukti akurasi (aturan 4 checklist master). Kolom
-- actual_value diisi MENYUSUL oleh proses backfill (item 6·04).
--
-- Cara apply:
--   psql "$DATABASE_URL" -f db/migrations/001_forecast_and_actual.sql
--   (atau: Supabase SQL Editor → tempel seluruh berkas → Run)
-- Idempotent: aman dijalankan berulang.
--
-- ── Penyesuaian terhadap template checklist master (disebabkan kontrak §4) ──
-- 1. forecast_kw/actual_kw → forecast_value/actual_value
--    Target aktif v1.0.0 = irradiance (W/m²), BUKAN daya kW. Kolom kw akan
--    salah satuan sejak baris pertama; daya/energi menyusul via gate §10.2/10.3.
-- 2. horizon int → horizon_step int
--    Dalam satu batch, horizon total sama untuk SEMUA baris (= 7) — kolom itu
--    redundan per baris. Yang dibutuhkan akurasi per-horizon (8·07) adalah
--    nomor langkah baris itu (1..30). Totalnya tetap bisa dihitung per batch.
-- 3. Kolom tambahan: forecast_id (acuan batch §5.3), ts (asal perhitungan
--    jendela horizon), target/resolution/unit (biar analisis tak perlu join).
-- Tidak diubah: aturan "tidak pernah menimpa", UNIQUE(site, generated_at,
-- forecast_time), timestamptz, index (site_id, forecast_time).
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS forecast_result (
    id            bigserial PRIMARY KEY,
    forecast_id   uuid         NOT NULL,          -- batch: 1 forecast_id → horizon baris
    site_id       text         NOT NULL,
    target        text         NOT NULL DEFAULT 'irradiance',
    resolution    text         NOT NULL DEFAULT 'daily',
    unit          text         NOT NULL DEFAULT 'W/m2',
    ts            timestamptz  NOT NULL,          -- asal forecast (origin jendela)
    forecast_time timestamptz  NOT NULL,          -- waktu YANG DIPREDIKSI
    generated_at  timestamptz  NOT NULL,          -- waktu forecast DIBUAT
    forecast_value double precision NOT NULL,     -- W/m² (3 desimal di API)
    actual_value  double precision,               -- diisi menyusul (6·04); NULL = belum tiba
    horizon_step  int          NOT NULL,          -- 1..30, nomor langkah dari ts
    model_version text         NOT NULL,
    data_version  text,                           -- versi dataset sumber (nullable)
    -- TIDAK PERNAH MENIMPA: baris generasi baru = row baru, unik per batch.
    CONSTRAINT uq_forecast_result
        UNIQUE (site_id, generated_at, forecast_time),
    CONSTRAINT uq_forecast_result_batch
        UNIQUE (forecast_id, forecast_time),
    CONSTRAINT ck_forecast_result_step
        CHECK (horizon_step BETWEEN 1 AND 30),
    CONSTRAINT ck_forecast_result_value
        CHECK (forecast_value >= 0),
    CONSTRAINT ck_forecast_result_actual_ck
        CHECK (actual_value IS NULL OR actual_value >= 0)
);

-- Baca monitoring: grafik per site pada rentang forecast_time (hot path 5·05).
CREATE INDEX IF NOT EXISTS ix_forecast_result_site_time
    ON forecast_result (site_id, forecast_time);

-- Riwayat per generasi (endpoint history §5.3: urut generated_at DESC).
CREATE INDEX IF NOT EXISTS ix_forecast_result_site_generated
    ON forecast_result (site_id, generated_at DESC, forecast_id ASC);

-- Antrian backfill 6·04: hanya baris yang actual_value-nya masih NULL.
CREATE INDEX IF NOT EXISTS ix_forecast_result_pending_actual
    ON forecast_result (site_id, forecast_time)
    WHERE actual_value IS NULL;

COMMENT ON TABLE forecast_result IS
    'Satu baris per forecast per timestamp; write-once, tidak pernah ditimpa. actual_value diisi menyusul (6·04).';
COMMENT ON COLUMN forecast_result.generated_at IS
    'Waktu pembuatan batch; SLA monitoring: generated_at lebih tua dari 90 menit = layanan bermasalah (kontrak §5.2).';
COMMENT ON COLUMN forecast_result.actual_value IS
    'NULL = waktu belum tiba / belum di-backfill. Backfill hanya BOLEH mengisi NULL, tidak boleh mengubah yang terisi.';

-- ── TABEL ACTUAL — deret pengamatan per site ─────────────────────────────
-- Sumber: data/actuals.csv (app/actuals.py, kolom actual_irradiance W/m²).
-- Berbeda forecast: pengamatan boleh di-upsert (koreksi re-ingest = data yang
-- sama untuk site+ts yang sama). Dedupe per pengamatan, bukan per batch.
CREATE TABLE IF NOT EXISTS actual_result (
    id           bigserial PRIMARY KEY,
    site_id      text         NOT NULL,
    ts           timestamptz  NOT NULL,           -- WAKTU PENGAMATAN (bukan prediksi)
    actual_value double precision NOT NULL,       -- W/m²
    unit         text         NOT NULL DEFAULT 'W/m2',
    source       text         NOT NULL DEFAULT 'data/actuals.csv',
    observed_at  timestamptz  NOT NULL DEFAULT now(),  -- kapan baris ini masuk
    CONSTRAINT uq_actual_result
        UNIQUE (site_id, ts),
    CONSTRAINT ck_actual_result_value
        CHECK (actual_value >= 0)
);

CREATE INDEX IF NOT EXISTS ix_actual_result_site_time
    ON actual_result (site_id, ts);

COMMENT ON TABLE actual_result IS
    'Deret aktual per site; upsert per (site_id, ts). Dipakai backfill forecast_result.actual_value dan perhitungan akurasi (8·07).';

-- ── CATATAN PENERAPAN ────────────────────────────────────────────────────
-- * Belum ada yang menulis ke tabel ini: 6·02 (store → baca DB) dan 6·03
--   (scheduler → simpan per run) menyusul. Hingga itu, latest/history tetap
--   membaca InMemoryForecastStore — kontrak tidak berubah sama sekali.
-- * DATABASE_URL disimpan di environment (6·05), tidak di kode/.env ter-commit.
-- * Setelah 6·02: jalankan ulang test kontrak Fase 5 (6·07) — wajib lulus
--   tanpa perubahan field.
