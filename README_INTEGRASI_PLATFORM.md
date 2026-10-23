# Cara Memanggil API Forecasting — Untuk Tim Platform Monitoring

**Base URL:** `https://ocdi.blitz.cloud`
**Versi:** `v1` (di URL, bukan di header) · **Zona waktu site:** `Asia/Makassar` (WITA, UTC+8)
**Kontrak lengkap:** `KONTRAK_API_v1.md` di repo `forecast-service` — halaman ini ringkasannya.

---

## 1. Cara Memanggil (ringkas)

Semua endpoint **kecuali `/health`** butuh header:

```
X-API-Key: <key Anda>
```

`/health` sengaja terbuka (probe uptime) dan hanya mengembalikan field minimal.
Cara paling cepat memverifikasi key Anda:

```bash
curl https://ocdi.blitz.cloud/api/v1/health                 # 200 = service hidup
curl https://ocdi.blitz.cloud/api/v1/forecast/latest?site_id=PLTS_KUPANG \
     -H "X-API-Key: <key Anda>"                             # 200 = key sah
```

- Key salah / tidak ada / sudah dicabut → **403** (atau 401 bila header hilang). Simpan key
  di **secret manager** — bukan source code, URL, HTML, atau chat.
- Setiap konsumen punya key sendiri, jadi pemakaian & pencabutan per konsumen.

---

## 2. Endpoint yang dipakai monitoring

| # | Method + Path | Fungsi | Auth |
|---|---|---|---|
| 1 | `GET /api/v1/health` | Uptime probe | Tidak |
| 2 | `GET /api/v1/forecast/latest?site_id=…` | Forecast terbaru — **dipakai grafik** | Ya |
| 3 | `GET /api/v1/forecast/history?site_id=…&from=…&to=…&limit=…` | Riwayat per generasi | Ya |
| 4 | `GET /api/v1/model/performance?site_id=…` | MAE/RMSE/MAPE/skill per horizon | Ya |
| 5 | `GET /api/v1/model/info` | Metadata model | Ya |
| 6 | `GET /api/v1/forecast/actual?site_id=…&from=…&to=…` | Deret aktual (pembanding) | Ya |
| 7 | `POST /api/v1/forecast` | Forecast on-demand (uji engineer — **jangan dipakai polling**) | Ya |

**`site_id` yang tersedia:** `PLTS_KUPANG` (tambah = konfigurasi server, non-breaking).
**`target` yang tersedia:** `irradiance` (unit `W/m2`) — `energy_kwh` / `power_kw` menyusul (kontrak §10).

---

## 3. Contoh Request & Response (asli, live)

### 3.1 `GET /api/v1/health` — 200 (diambil dari production, 16 Okt 2026)

```json
{
  "status": "ok",
  "service_version": "v1.0",
  "timestamp": "2026-10-16T12:22:50+08:00",
  "uptime_seconds": 8450,
  "model_loaded": true
}
```

### 3.2 `GET /api/v1/forecast/latest?site_id=PLTS_KUPANG` — 200

> Struktur dari kontrak §5.2. **Selalu baca `ts` dan `generated_at`** — keduanya
> menentukan apakah isi masih boleh dipercaya (lihat §5).

```json
{
  "forecast_id": "6b1f0c2e-…",
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "resolution": "daily",
  "unit": "W/m2",
  "timezone": "Asia/Makassar",
  "ts": "2025-12-31T00:00:00+08:00",
  "generated_at": "2026-10-01T06:00:00+08:00",
  "horizon": 7,
  "model_version": "v1.0.0",
  "forecast": [
    { "forecast_time": "2026-01-01T00:00:00+08:00", "forecast_value": 212.284, "model_version": "v1.0.0" },
    { "forecast_time": "2026-01-02T00:00:00+08:00", "forecast_value": 205.617, "model_version": "v1.0.0" }
  ]
}
```

**Tanpa parameter `horizon`** — endpoint ini membaca apa yang tersimpan (scheduler
menghasilkan horizon 7). Parameter tambahan yang tidak dikenal diabaikan, bukan error.

### 3.3 Contoh terjemahan via adapter (yang dipakai di UI)

Ambil adapter di `GET /static/monitoring-adapter.js` lalu:

```js
import { toForecast } from "./monitoring-adapter.js";
const m = toForecast(await res.json());
m.points        // [{ when, value, inHorizon }, …] — inHorizon=false = jangan digambar
m.validUntil    // batas akhir jendela horizon (YYYY-MM-DD, offset +08:00)
m.generatedAt   // umur data → deteksi basi
```

**Ini satu-satunya tempat membaca nama field kontrak.** Kalau kontrak berubah,
hanya berkas itu yang disentuh — UI Anda tidak ikut rusak.

---

## 4. Kode Error

**Jangan pernah parse `message`** — pakai `error.code`. Setiap error punya envelope:

```json
{ "error": { "code": "E_FORBIDDEN", "message": "invalid or revoked API key", "request_id": "57342780509147c193a6932aed63301c" } }
```

| Kode | HTTP | Arti |
|---|---|---|
| `E_UNAUTHORIZED` | 401 | Header `X-API-Key` tidak ada |
| `E_FORBIDDEN` | 403 | Key salah / dicabut |
| `E_VALIDATION_FAILED` | 422 | Body/query gagal validasi schema |
| `E_HORIZON_RANGE` | 422 | `horizon` di luar 1..30 |
| `E_TZ_REQUIRED` | 422 | Timestamp tanpa offset (`+08:00`) — zona tidak pernah ditebak |
| `E_HISTORY_TOO_SHORT` | 422 | `history` < 30 item (hanya `POST /forecast`) |
| `E_IRREGULAR_SERIES` | 422 | Langkah waktu tidak seragam (hanya `POST /forecast`) |
| `E_DUPLICATE_TIMESTAMP` | 422 | Ada `timestamp` ganda (hanya `POST /forecast`) |
| `E_VALUE_INVALID` | 422 | `value` null / bukan angka / negatif |
| `E_TARGET_UNAVAILABLE` | 422 | `target` ada di enum tapi belum aktif |
| `E_EXOG_INCOMPLETE` / `E_EXOG_MISSING_COLUMN` | 422 | `exog` tidak lengkap |
| `E_TIME_RANGE_INVALID` | 422 | `to < from` (history, actual) |
| `E_LIMIT_RANGE` | 422 | `limit` di luar 1..100 (history) |
| `E_CURSOR_INVALID` | 400 | Cursor rusak / kedaluwarsa (history) |
| `E_CURSOR_FILTER_MISMATCH` | 422 | Filter berubah di tengah paginasi |
| `E_IDEMPOTENCY_MISMATCH` | 409 | `Idempotency-Key` sama, body berbeda (hanya `POST /forecast`) |
| `E_SITE_UNKNOWN` | 404 | `site_id` tidak dikenal service |
| `E_NO_FORECAST` | 404 | Site valid, forecast **belum pernah** dibuat |
| `E_RATE_LIMITED` | 429 | Kuota terlampaui |
| `E_INTERNAL` | 500 | Galat tak terduga (jangan bocorkan detail ke pengguna) |
| `E_SERVICE_UNAVAILABLE` | 503 | Model / file data belum siap |
| `E_MODEL_FAILED` | 500 | ARIMA gagal konvergen (hanya `POST /forecast`) |

Setiap respons menyertakan `X-Request-Id` di header **dan** body error —
lampirkan ID ini saat melaporkan masalah.

**Cara membedakan 404:** `E_NO_FORECAST` = site valid, scheduler belum pernah
mengisi forecast; `E_SITE_UNKNOWN` = site tidak ada di konfigurasi.

---

## 5. Batas Pemakaian

| Aspek | Nilai |
|---|---|
| Rate limit | **60 request/menit per API key** (semua endpoint non-health) |
| Header pada 429 | `Retry-After` (detik), `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` |
| Header pada sukses | `X-RateLimit-Limit`, `X-RateLimit-Remaining` — pantau sisa kuota |
| Cache `latest` | `Cache-Control: max-age=60` (paling segar 1 menit) |
| Cache `model/info`, `model/performance` | `Cache-Control: max-age=300` |
| Cache `health` | `Cache-Control: no-store` |
| Regenerasi scheduler | **per jam** — forecast paling baru bisa berumur ≤ 60 menit |

**Rekomendasi interval polling platform:** 15 menit untuk `latest`, 30 menit untuk
`performance`, 60 menit untuk `history`. Satu platform = satu key = satu kuota 60/menit
— **jangan polling per-pengguna browser.**

---

## 6. Perilaku Saat Service Tidak Tersedia / Forecast Kedaluarsa

Ringkasan keputusan lengkap: `KEPUTUSAN_5_07_KADALUARSA.md`.

- Forecast **berlaku selama `now ≤ ts + horizon hari`** — setelah itu garis forecast
  **dihapus total** dari UI, tampil badge **KEDALUARSA** (bukan dipertahankan redup).
- **Service tidak tersedia < 2 menit:** tampilkan cache (umur < 1 jam) sementara,
  retry 5s → 15s → 30s, **tanpa** badge "BERLAKU".
- **Service tidak tersedia 2–60 menit:** hapus garis forecast; cache hanya
  dipertahankan bila `now - generated_at ≤ 1 jam`, dengan label `data terakhir … mungkin sudah kedaluwarsa`.
- **Service tidak tersedia > 60 menit:** hapus semua nilai & garis; tombol coba ulang manual.
- **`generated_at` tertinggal > 6 jam:** forecast dianggap basi meski masih di dalam horizon.
- **Semua state gagal harus menampilkan pesan, bukan angka kosong/basi.**

---

## 7. Zona Waktu (titik integrasi paling rawan)

- Semua timestamp respons memakai **`+08:00`** (WITA) — **jangan dikonversi ke zona browser**.
- Request dengan timestamp tanpa offset → `422 E_TZ_REQUIRED` (zona tidak pernah ditebak).
- Untuk perhitungan (urutan, join, perbandingan), konversi dulu ke UTC — jangan bandingkan string offset berbeda.

---

## 8. Siapa yang Dihubungi

| Situasi | Kontak |
|---|---|
| Butuh API key baru / dicabut / diganti | Dev forecast — via tim platform |
| Bug API / response tidak sesuai kontrak | Dev forecast, **lampirkan `X-Request-Id`** |
| Forecast terlihat basi / scheduler tidak jalan | Dev forecast |
| Minta tambah `site_id` baru | Dev forecast |

**Sebelum melaporkan, siapkan:** `X-Request-Id`, `site_id`, waktu kejadian (dengan
`+08:00`), URL endpoint, dan potongan request/response.

---

## 9. Catatan Stabilitas

- **Status kontrak:** `KONTRAK_API_v1.md` masih **DRAFT** (§10 belum freeze — target
  final, sumber `power_history`, kalibrasi, zona waktu, rate limit). Field di atas sudah
  berjalan di production dan dikunci lewat test kontrak, tapi **sebelum freeze**
  perubahan field masih mungkin.
- **Storage forecast masih in-memory** — hilang saat restart; scheduler mengisinya
  ulang otomatis, tapi riwayat panjang (untuk akurasi berjalan) baru setelah Fase 6 (DB).
- **Service free tier bisa cold start 30–60 detik** saat tertidur — tangani sebagai
  `503` / retry, bukan error permanen.
