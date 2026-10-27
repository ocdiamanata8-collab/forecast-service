-- ═══════════════════════════════════════════════════════════════════════
-- 003 — STATE ALERT FORECAST   · 7·04 (visibilitas kegagalan)
-- SEI Forecasting Service · PostgreSQL / Supabase
--
-- State durable untuk deduplikasi/cooldown alert per site + kondisi.
-- forecast_run menyimpan setiap run, tabel ini menyimpan state notifikasi
-- (aktif, terakhir dikirim). Tanpa state durable, monitor 15 menit akan
-- mengirim pesan berulang pada setiap polling.
--
-- Kondisi alert terpisah:
--   consecutive_failure — tiga run gagal berturut-turut per site.
--   stale_run           — tidak ada run sukses dalam 3 jam (termasuk cron
--                         yang tidak berjalan sama sekali; tidak ada row run).
--
-- Tidak ada FK ke forecast_run: state harus hidup meski belum ada run.
-- `first_seen_at` memulai grace 3 jam bila site baru / belum pernah berjalan.
--
-- Cara apply:
--   psql "$DATABASE_URL" -f db/migrations/003_forecast_alert_state.sql
-- Idempotent: aman dijalankan berulang.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS forecast_alert_state (
    site_id          text        NOT NULL,
    alert_type       text        NOT NULL,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    active           boolean     NOT NULL DEFAULT false,
    last_notified_at timestamptz,
    recovered_at     timestamptz,
    PRIMARY KEY (site_id, alert_type),
    CONSTRAINT ck_forecast_alert_type
        CHECK (alert_type IN ('consecutive_failure', 'stale_run'))
);

COMMENT ON TABLE forecast_alert_state IS
    'State alert durable per site/kondisi: dedupe, cooldown 6 jam, dan recovery untuk monitor 7·04.';
COMMENT ON COLUMN forecast_alert_state.first_seen_at IS
    'Awal grace 3 jam sebelum site tanpa riwayat run dianggap stale.';
COMMENT ON COLUMN forecast_alert_state.last_notified_at IS
    'Waktu notifikasi terakhir yang sukses terkirim; cooldown mencegah spam.';
