# Deploy ke Render (free tier PoC)

Service ini sudah siap dideploy. Konfigurasi build/start ada di
[`render.yaml`](render.yaml), dan `Dockerfile` juga tersedia bila ingin
memakai runtime container.

## Peringatan: free tier tidur

Instance free tidur setelah **±15 menit tanpa traffic**, lalu butuh
**±30-60 detik cold start**. Untuk PoC/demo internal, ini wajar. Untuk
monitoring operasional 24/7, tidak layak — lihat `KONTRAK_API_v1.md` §10.

## Build & start command

| | Command |
|---|---|
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |

`requirements.txt` adalah **satu-satunya sumber dependency** — tidak ada
`pyproject.toml`, tidak ada pin tersembunyi di tempat lain. Start command
memakai `$PORT` yang diinjeksi Render; uvicorn **tidak** membaca `$PORT`
sendiri, jadi harus dieksplisit seperti di atas.

Versi Python: **3.13** — sama dengan yang dipakai untuk mengunci
`requirements.txt` dan menjalankan seluruh suite. Jangan diturunkan ke 3.12
tanpa menguji ulang (termasuk `tests/test_engine_golden.py` dan
`tests/test_module_parity.py`).

## Langkah deploy ke Render

1. Push repo ke GitHub (folder `forecast-service/` jadi root repo).
2. Buka [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**.
3. Pilih repo GitHub → Render mendeteksi `render.yaml` secara otomatis.
4. **Isi environment variable `API_KEYS`** sebelum service pertama kali
   start. Tanpa ini, semua endpoint ber-key membalas `401` (by design —
   lihat `app/main.py`).

   Format **per konsumen** (kontrak §1.1) — satu konsumen satu key + label:

   ```
   API_KEYS=<label1>:<kunci1>,<label2>:<kunci2>
   # contoh: API_KEYS=monitoring:<kunci>,ci:<kunci>
   ```

   - Label = identitas konsumen (huruf kecil, 2–32 karakter) — dipakai log
     pemakaian (`consumer=monitoring ...`) dan pencabutan per konsumen.
   - Generate **satu kunci baru per konsumen** (43 karakter — di atas
     minimum 16):
     ```powershell
     python -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
   - **Simpan kunci di secret manager milik konsumen itu sendiri** (mis.
     environment panel Blitz monitoring, vault CI) — bukan di repository,
     URL, file HTML, atau chat. `.env` sudah di-gitignore; `.env.example`
     hanya template placeholder.
   - Pencabutan satu konsumen: hapus `<label>:<kunci>` dari `API_KEYS` →
     deploy ulang → key itu `403`, konsumen lain tidak terpengaruh.

   **Yang sengaja TIDAK ada di env** (lihat `.env.example` §2):

   | Var | Kenapa tidak dipakai |
   |---|---|
   | `MODEL_VERSION` | Versi hidup di `model/VERSION` — SATU-SATUNYA sumber (diuji `test_version.py`). Menambah env versi kedua hanya menciptakan sumber kebenaran ganda |
   | `TZ` | Zona dikunci `Asia/Makassar` (WITA, UTC+8) di kode. `Asia/Jakarta` (UTC+7) akan menggeser seluruh timestamp 1 jam dan membuat filter 07:00–17:00 salah — persis kekeliruan zona yang sudah diperangi |

4b. **Database (Fase 6 · 6·05) — `DATABASE_URL`**. Isi dari dashboard Render
   (Environment Variables), **bukan** dari repository — string koneksi adalah
   rahasia seperti API key.

   ```
   DATABASE_URL=postgresql://user:pass@host:5432/forecast
   ```

   - **Sebelum start pertama** dengan DSN aktif, jalankan migration sekali:
     `001_forecast_and_actual.sql` lalu `002_forecast_run.sql`
     (folder `db/migrations/`). Tanpa tabel, startup sengaja **gagal cepat**
     (`DatabaseUnavailable`) — tanpa fallback diam-diam ke in-memory.
   - **`DATABASE_READONLY_URL` (opsional, 6·05)** — DSN user dengan hak baca
     saja; jalur `latest`/`history` (dashboard) memakainya, terpisah dari
     kredensial tulis. Kosongkan bila platform belum menyediakan user
     read-only: semua operasi memakai `DATABASE_URL` (perilaku lama).
     Pemisahan hak dilakukan di sisi database (grant SELECT vs tulis).
   - **Mengosongkan `DATABASE_URL`** = kembali ke PoC in-memory (hilang saat
     restart; backfill aktual & run log nonaktif).
   - **Backup (6·06) — `scripts/backup_db.py`**. Berbeda dengan bagian lain,
     ini **bukan env var**: script opsional yang memakai kredensial
     `DATABASE_URL` yang sudah ada, jadi tidak ada rahasia baru untuk
     dikelola. Prinsipnya sama dengan retensi baris: fitur mati sampai
     pemanggilnya ada.
     - `python scripts/backup_db.py --out backups` → satu backup
       `forecast-<stempel>.dump.gz` (format custom pg_dump, dikompresi gzip).
     - Host tanpa klien PostgreSQL (hanya Docker Desktop) → tambah
       `--via-docker --container forecast-pg`: `pg_dump` dijalankan di dalam
       container postgres.
     - **Retensi default MATI** (`--keep-days 0`): file lama tidak dihapus
       sampai Anda menyebutkannya. Menghapus backup tidak bisa dibatalkan,
       jadi tidak boleh terjadi diam-diam.
     - Cron harian (03:00, simpan 30 hari):

       ```cron
       0 3 * * * cd /srv/forecast-service && DATABASE_URL=... \
         python scripts/backup_db.py --out backups --keep-days 30 >> backups/backup.log 2>&1
       ```

     - Password tidak pernah masuk argumen proses (terlihat di `ps`);
       script menaruhnya lewat env `PGPASSWORD` hanya untuk proses `pg_dump`.
     - Folder `backups/` sudah di-gitignore — backup diperlakukan seperti
       kredensial, bukan seperti artefak yang boleh di-commit.
5. Deploy. Health check otomatis memakai `/api/v1/health`.
6. Verifikasi: `https://<nama-service>.onrender.com/api/v1/health`
   harus membalas `{"status":"ok", ...}`.

## Catatan penting untuk PoC

- **Persistensi hilang saat restart**: `forecast/latest` dan
  `forecast/history` memakai penyimpanan in-memory. Tapi scheduler Fase 4
  mengisi ulang **otomatis saat startup** (tick pertama + regenerasi per
  jam) dari tail `data/actuals.csv` — `GET /forecast/latest` langsung 200
  dalam hitungan detik setelah deploy tanpa perlu memanggil
  `POST /forecast` dulu (kontrak §5.1: POST on-demand memang tidak
  menyimpan).
- **Rate limit 60 request/menit per key** (kontrak §1.3) — cukup untuk
  PoC, tidak untuk polling ketat.
- **Data aktual (2023–2025) sudah termuat** dari `data/actuals.csv` —
  endpoint `/forecast/actual` langsung berfungsi.
- **API key hanya dari environment** — tidak ada fallback key di kode.
  Bila `API_KEYS` kosong, `401 E_UNAUTHORIZED` memang disengaja (lihat
  kebijakan repo hygiene).

## Alternatif: deploy dari Docker image

Jika ingin memakai image yang sudah teruji lokal (lihat `README.md`
bagian Docker), push image ke registry (Docker Hub/GHCR) lalu deploy dari
image — tidak perlu `render.yaml`, cukup Docker Runtime di Render.

## Verifikasi pasca-deploy

```bash
# Health
curl https://<nama>.onrender.com/api/v1/health

# Forecast on-demand (butuh API key)
curl -X POST https://<nama>.onrender.com/api/v1/forecast \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <kunci>" \
  -d '{"site_id":"PLTS_KUPANG","horizon":7,"history":[...]}'

# Swagger (terbuka tanpa key)
# Buka https://<nama>.onrender.com/docs
```

## Verifikasi pasca-deploy: field-per-field, bukan cuma 200

Script `scripts/validate_response_fields.py` memvalidasi **isi response**
terhadap kontrak — bukan hanya status code. "HTTP 200 tetapi field salah"
adalah kegagalan integrasi yang paling sering terjadi dan paling sulit
ditemukan.

```bash
# Ganti <domain> dengan URL publik dan <kunci> dengan API key yang valid
python scripts/validate_response_fields.py \
  --base https://<domain>.onrender.com \
  --key <kunci>
```

Dilakukan terhadap container lokal di host ini, hasilnya **20/20 lulus**:
health (tanpa `model_version`), field konteks POST `/forecast`
(`site_id`/`unit`/`timezone`/`model_version`/`ts`/`generated_at`),
panjang forecast == horizon, timestamp `+08:00`, deterministik, `401`
tanpa key, dan envelope `422` + `request_id`.

**Contoh request yang salah** (dari instruksi awal): field
`historical_data` (bentuk `[float]`) ditolak `422 E_HISTORY_TOO_SHORT`
karena skema benar memakai `history: [{timestamp, value}]`. Contoh
valid lengkap terdokumentasi di `KONTRAK_API_v1.md` §5.1.
