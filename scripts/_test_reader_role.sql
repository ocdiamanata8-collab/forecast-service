-- UJI 6·05 — role baca-saja untuk memverifikasi pemisahan pool baca/tulis.
-- HANYA untuk container uji (forecast-pg); jangan dijalankan di produksi
-- tanpa keputusan grant yang sesuai kebijakan database Anda.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
        CREATE ROLE reader LOGIN PASSWORD 'read' NOSUPERUSER;
    END IF;
END
$$;
GRANT CONNECT ON DATABASE forecast TO reader;
GRANT USAGE ON SCHEMA public TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO reader;
