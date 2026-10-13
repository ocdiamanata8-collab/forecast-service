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
   - Generate kunci lokal:
     ```powershell
     python -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
   - Tempel di **Environment** → `API_KEYS` (Value dari `render.yaml`
     sengaja dibiarkan kosong / `sync: false`).
5. Deploy. Health check otomatis memakai `/api/v1/health`.
6. Verifikasi: `https://<nama-service>.onrender.com/api/v1/health`
   harus membalas `{"status":"ok", ...}`.

## Catatan penting untuk PoC

- **Persistensi hilang saat restart**: `forecast/latest` dan
  `forecast/history` memakai penyimpanan in-memory. Setelah deploy, panggil
  `POST /api/v1/forecast` dulu sebelum `GET /forecast/latest` berfungsi —
  endpoint itu memang dirancang tidak menyimpan (kontrak §5.1).
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
