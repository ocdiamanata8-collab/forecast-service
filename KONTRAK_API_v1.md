# KONTRAK API — Forecasting Service PLTS Kupang (v1)

| | |
|---|---|
| Dokumen | `KONTRAK_API_v1.md` |
| Versi kontrak | **v1.0.0** |
| Tanggal | 2026-10-01 |
| Status | **DRAFT — belum boleh diintegrasikan sebelum §10 disepakati** |
| Aturan waktu global | Timestamp masuk selalu membawa offset eksplisit; timestamp naive ditolak. Sistem menormalisasi semua instan ke **UTC untuk penyimpanan/perbandingan**, lalu serialisasi respons dengan zona site yang dikonfigurasi. |
| Zona waktu default site | **Kupang: `Asia/Makassar` (UTC+8).** Timestamp input `+07:00` (WIB) dikonversi ke UTC, bukan dipakai sebagai zona respons Kupang. |
| Lapisan terkait | [SPESIFIKASI_KONTRAK_MODEL.md](SPESIFIKASI_KONTRAK_MODEL.md) (kontrak data model: field, satuan, rentang, horizon) |
| Konsumen | Platform monitoring SEI (dan client lain melalui `X-API-Key`) |

> **Aturan emas:** dokumen ini disepakati **sebelum** platform monitoring mulai memanggil.
> Setelah itu, setiap perubahan yang melanggar §9 = **breaking change** = wajib naik
> versi mayor (`/api/v2/...`) atau wajib persetujuan ulang semua konsumen.

---

## 1. ATURAN UMUM

| Aspek | Keputusan |
|---|---|
| Base path | `/api/v1` — versi di URL, bukan di header |
| Media type | `application/json; charset=utf-8` |
| Timestamp | RFC 3339 **wajib tz-aware**: `2026-10-01T00:00:00+08:00` (offset eksplisit; `+08:00` untuk site Kupang) |
| Zona waktu layanan | Sistemik: instan timestamp dinormalisasi ke UTC untuk penyimpanan/perbandingan; serialisasi respons memakai zona IANA site. **Kupang: `Asia/Makassar` (WITA, UTC+8).** |
| Input multi-zona | Offset input boleh berbeda (`Z`, `+07:00`, `+08:00`); semua dikonversi ke UTC sebelum alignment. Timestamp tanpa offset ditolak. Zona respons = zona site dari konfigurasi, tidak disimpulkan dari offset request. |
| Angka desimal | Semua nilai forecast **3 angka di belakang koma**; metrik MAE/RMSE 3 desimal, MAPE 2 desimal |
| Null vs hilang | Field wajib **tidak boleh** `null`; field opsional boleh dihilangkan dari JSON (bukan `null`) bila kosong |
| Bahasa pesan error | Inggris, stabil; pesan lengkap untuk manusia, `code` untuk mesin — jangan pernah parse `message` |
| Header respons | `X-Request-Id: <uuid>` di **semua** respons (termasuk error) untuk penelusuran log |

### 1.1 Autentikasi

```
X-API-Key: <key>          ← wajib pada semua endpoint KECUALI GET /api/v1/health
```

- Tanpa header → `401`, kode `E_UNAUTHORIZED`.
- Key salah/dicabut → `403`, kode `E_FORBIDDEN`.
- `GET /api/v1/health` sengaja **terbuka** (tanpa key) agar probe uptime external bisa memantaunya; responsnya hanya memuat field minimal (§6.6), tanpa info model detail.

**Satu konsumen, satu key.** Setiap konsumen (platform monitoring, CI,
engineer) memegang key sendiri sehingga pemakaian **dipantau per konsumen**
dan dapat **dicabut per konsumen** tanpa memutus pihak lain. Konfigurasi
server memakai format env `API_KEYS`:

```
API_KEYS=<label1>:<key1>,<label2>:<key2>
         # contoh: monitoring:<token>,ci:<token>
```

- `label` = identitas konsumen (`^[a-z][a-z0-9_-]{1,31}$`) — dipakai untuk
  **log pemakaian** server (`consumer=<label> ...` per request; nilai key
  tidak pernah dicatat) dan operasi revoke. Label **bukan** bagian dari
  kontrak HTTP: header tetap hanya `<key>` tanpa prefix.
- Entri tanpa label (`API_KEYS=<key>`) tetap valid untuk kompatibilitas —
  label menjadi `legacy`.
- Verifikasi tetap lookup key terhadap env `API_KEYS` (tidak ada fallback
  key di kode, tidak ada klaim/scope tambahan — auth v1 biner: ada key sah
  atau tidak).
- **Penyimpanan key**: setiap konsumen menyimpan key miliknya di secret
  manager miliknya sendiri (mis. environment/secret manager platform
  monitoring) — **bukan** di source code, URL, file HTML, atau chat.
- **Pencabutan per konsumen**: hapus entri `<label>:<key>` dari `API_KEYS`
  → deploy/restart → key itu `403 E_FORBIDDEN`, konsumen lain tak
  terpengaruh. (Registry dibangun ulang saat startup — tanpa persistensi
  tidak ada jalur revoke tanpa restart.)
- **Rotasi**: tambahkan key baru untuk konsumen itu → pastikan konsumen
  memakai key baru → hapus key lama dari `API_KEYS` → restart.

### 1.2 Idempotensi & keamanan ulang panggilan

| Method | Idempoten? | Catatan |
|---|---|---|
| `POST /api/v1/forecast` | Ya (secara praktis) | Mesin deterministik: input identik → output identik. Header opsional `Idempotency-Key: <string>` disimpan 24 jam; key sama + body sama → respons pertama dikembalikan ulang tanpa komputasi ulang; key sama + body **beda** → `409`, kode `E_IDEMPOTENCY_MISMATCH` |
| Semua `GET` | Ya | Tidak ada efek samping; tidak butuh key idempotensi |

### 1.3 Rate limit

- **60 request/menit per API key** untuk semua endpoint non-health — per key
  = per konsumen, jadi laju satu konsumen tidak pernah menguras kuota konsumen lain.
- Respons **429** menyertakan header: `Retry-After: <detik>`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- Respons **sukses** menyertakan `X-RateLimit-Limit` dan `X-RateLimit-Remaining`
  (kuota tersisa untuk key itu) — konsumen bisa memantau sisa kuota sebelum
  terkena 429. Menambah header = non-breaking (§9.2).
- Body `429` memakai envelope error standar (§5) dengan kode `E_RATE_LIMITED`.

### 1.4 Caching

| Endpoint | Header |
|---|---|
| `GET .../forecast/latest` | `Cache-Control: max-age=60` (maksimal 1 menit; forecast diregenerasi minimal per jam) |
| `GET .../model/info`, `GET .../model/performance` | `Cache-Control: max-age=300` |
| `GET .../health` | `Cache-Control: no-store` |
| `POST .../forecast` | `Cache-Control: no-store` |

### 1.5 Aturan Zona Waktu Tunggal (WAJIB — titik integrasi paling rawan)

Semua timestamp di seluruh sistem mengikuti tiga aturan ini tanpa kecuali:

1. **Offset eksplisit, selalu.** Tidak ada timestamp tanpa zona di request,
   respons, maupun penyimpanan. Naive timestamp → `422 E_TZ_REQUIRED`.
   Zona tidak pernah ditebak dari format atau header client.
2. **Penyimpanan & perbandingan dalam UTC.** Setiap instan dikonversi ke UTC
   sebelum disimpan, di-join, atau dibandingkan. Dengan begitu history SCADA
   (`Z`), inverter (`+07:00`), dan cuaca (`+08:00`) selaras walau zona asalnya
   berbeda. Urutan dan interval dihitung pada garis UTC.
3. **Serialisasi respons dalam zona site.** Field `timezone` menyatakan IANA
   zone; setiap timestamp keluar dibawa dengan offset aktif zona tersebut.
   Site Kupang → `Asia/Makassar` → `+08:00`.

**Kenapa ini aturan, bukan preferensi:** field `generated_at` vs `forecast_time`
dipakai untuk menghitung akurasi dan deteksi keterlambatan. Selisih zona yang
tidak disadari membuat forecast terlihat "selalu telat" atau "selalu cepat"
berjam-jam, dan model 1 jam off dari matahari pada filter siang 07:00–17:00.
Kesalahan ini tidak pernah gagal validasi — ia menghasilkan angka yang salah
dengan angka yang tampak benar.

| Sumber data | Zona khas | Penanganan |
|---|---|---|
| SCADA / EMS | `Z` (UTC) | Dikonversi ke UTC, disimpan UTC, diserialisasi zona site |
| Inverter / meter | `+07:00` (WIB) | Sama — offset dibaca dari payload, zona asal tidak disimpan sebagai kebenaran |
| Cuaca (ERA5/CAMS) | `Z` (UTC) | Sama; momen `00:00 Z` ≠ `00:00 +08:00` — jangan pernah menyamakan jam kalender lintas zona |
| Request client | offset apa pun | Dikonversi ke UTC; **offset respons tidak mengikuti offset request** |

Pemeriksaan otomatis: `python test_timezone_contract.py` — mencakup zona
keluar, konversi lintas zona, penolakan naive, dan kesetaraan modul/kontrak.

---

## 2. DAFTAR RESOURCE

| # | Method | Path | Fungsi | Auth | Idempoten |
|---|---|---|---|---|---|
| 1 | `POST` | `/api/v1/forecast` | Jalankan forecast on-demand | API key | Ya (deterministik + `Idempotency-Key`) |
| 2 | `GET` | `/api/v1/forecast/latest` | Hasil terakhir — **dipakai monitoring** | API key | Ya |
| 3 | `GET` | `/api/v1/forecast/history` | Riwayat forecast tersimpan | API key | Ya |
| 4 | `GET` | `/api/v1/model/info` | Versi & metadata model | API key | Ya |
| 5 | `GET` | `/api/v1/model/performance` | MAE / RMSE / MAPE | API key | Ya |
| 6 | `GET` | `/api/v1/health` | Status layanan | Terbuka | Ya |
| 7 | `GET` | `/api/v1/forecast/actual` | Data irradiance aktual per tanggal (grafik actual-vs-forecast engineer) | API key | Ya |

---

## 3. OBJECT BERSAMA

### 3.1 `ForecastPoint` — satu langkah forecast

| Field | Tipe | Satuan | Wajib | Ketentuan |
|---|---|---|---|---|
| `forecast_time` | string (RFC 3339) | — | Ya | Wajib offset eksplisit; respons Kupang memakai `+08:00`; target waktu, **bukan** waktu dibuat; urut naik; unik dalam satu set |
| `forecast_value` | number | mengikuti `unit` | Ya | 3 desimal; `>= 0` |
| `model_version` | string | — | Ya | Semver `^v\d+\.\d+(\.\d+)?$`; versi sumber tunggal: `model/VERSION` (baris pertama) |

> **Catatan penamaan:** field nilai memakai `forecast_value` (bukan `forecast_kw`)
> karena satuan mengikuti `unit` di tingkat respons. Keputusan satuan (`kW` vs `kWh`
> vs `W/m2`) terkunci di §4 — inilah sebabnya nama netral dipakai sejak awal,
> supaya penamaan tidak perlu berubah lagi setelah gate §10 diputuskan.

### 3.2 `ForecastSet` — satu rangkaian forecast

| Field | Tipe | Ketentuan |
|---|---|---|
| `site_id` | string | Pola `^[A-Z]{2,4}[_A-Z0-9]{2,12}$` |
| `target` | enum | v1.0.0: **hanya `"irradiance"`** (lihat §4) |
| `resolution` | enum | `"daily"` (v1.0.0); `"hourly"` ditambahkan kemudian = non-breaking |
| `unit` | enum | Mengikuti `target` — lihat tabel §4 |
| `timezone` | string | IANA zona **site**; untuk `PLTS_KUPANG` selalu `"Asia/Makassar"` di v1 |
| `ts` | string (RFC 3339) | Batas akhir **data input** yang dipakai |
| `generated_at` | string (RFC 3339) | Saat forecast **dibuat** — wajib terpisah dari `ts` agar akurasi bisa diukur |
| `horizon` | integer | 1..30 (daily) |
| `forecast` | array of `ForecastPoint` | Panjang **tepat** = `horizon` |
| `model_version` | string | Semver |
| `order` | string (opsional) | Mis. `"ARIMA(1,0,2)"` |
| `forecast_id` | string (opsional) | UUID — ada pada endpoint penyimpanan (`latest`, `history`), tidak ada pada `POST` on-demand |

### 3.3 `ErrorEnvelope` — bentuk semua error

```json
{
  "error": {
    "code": "E_HORIZON_RANGE",
    "message": "horizon must be between 1 and 30 for resolution=daily",
    "details": [
      { "field": "horizon", "issue": "out_of_range", "value": 45 }
    ],
    "request_id": "9f0c2e1a-4b7d-4c3e-8a1f-2d5e6f7a8b9c"
  }
}
```

| Field | Tipe | Wajib | Ketentuan |
|---|---|---|---|
| `error.code` | string | Ya | Kode stabil dari §6 — **inilah** yang diparse client |
| `error.message` | string | Ya | Kalimat Inggris untuk manusia; boleh berubah bebas |
| `error.details` | array | Tidak | Hanya untuk `422`; tiap item `{field, issue, value?}` |
| `error.request_id` | string | Ya | Sama dengan header `X-Request-Id` |

---

## 4. SATUAN — TABEL RESMI

| `target` | `unit` | Rentang | Status v1.0.0 |
|---|---|---|---|
| `irradiance` | `W/m2` | `0–1500` | ✅ Tersedia (satu-satunya yang dipublikasikan di v1.0.0) |
| `energy_kwh` | `kWh` | `0–kapasitas` | ⛔ Belum — menunggu gate §10.2 (Opsi A/B/C) dan §10.3 (kalibrasi) |
| `power_kw` | `kW` | `0–kapasitas` | ⛔ Belum — butuh data inverter (gate §10.2 Opsi C) |

**Aturan:** `unit` mengikuti `target`, dikunci per `target` di tabel ini.
`target` di request yang belum tersedia → `422`, kode `E_TARGET_UNAVAILABLE`.
Menambah baris `target` baru = **non-breaking** (additive). Mengubah `unit`
baris yang sudah ada = **breaking**.

---

## 5. ENDPOINT

### 5.1 `POST /api/v1/forecast` — jalankan on-demand

**Request**

```json
{
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "resolution": "daily",
  "horizon": 7,
  "history": [
    { "timestamp": "2024-01-01T00:00:00+08:00", "value": 186.4 },
    { "timestamp": "2024-01-02T00:00:00+08:00", "value": 191.2 }
  ],
  "exog": null
}
```

| Field | Tipe | Wajib | Validasi (melanggar → `422`) |
|---|---|---|---|
| `site_id` | string | Ya | Pola §3.2 |
| `target` | string | Ya | Harus tersedia di §4 |
| `resolution` | string | Tidak (default `daily`) | `daily` \| `hourly` |
| `horizon` | integer | Ya | 1..30 untuk `daily`; `E_HORIZON_RANGE` bila di luar |
| `history` | array | Ya | Minimal **30** item (`E_HISTORY_TOO_SHORT`); `timestamp` wajib tz-aware `+08:00` (`E_TZ_REQUIRED`); urut naik; langkah **seragam** (`E_IRREGULAR_SERIES`); `value` numerik, `>= 0`, tanpa `null` (`E_VALUE_INVALID`); tanpa timestamp duplikat (`E_DUPLICATE_TIMESTAMP`) |
| `exog` | object \| null | Tidak | `{columns: [...], points: [{timestamp, <kolom>: value}]}`; bila diisi, **wajib menutup** seluruh jendela forecast (`E_EXOG_INCOMPLETE`); kolom wajib ada di `columns` (`E_EXOG_MISSING_COLUMN`) |

Header opsional: `Idempotency-Key: <string>` (§1.2).

**Respons: `200 OK`**

```json
{
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "resolution": "daily",
  "unit": "W/m2",
  "timezone": "Asia/Makassar",
  "ts": "2025-12-31T00:00:00+08:00",
  "generated_at": "2026-10-01T09:15:02+08:00",
  "horizon": 7,
  "model_version": "v1.0.0",
  "order": "ARIMA(1,0,2)",
  "forecast": [
    { "forecast_time": "2026-01-01T00:00:00+08:00", "forecast_value": 212.284, "model_version": "v1.0.0" },
    { "forecast_time": "2026-01-02T00:00:00+08:00", "forecast_value": 211.631, "model_version": "v1.0.0" }
  ]
}
```

Endpoint ini **tidak menyimpan** hasil; penyimpanan terjadi di scheduler latar
belakang yang mengisi `latest`/`history`. Konsumen monitoring memakai endpoint #2,
bukan #1.

> **Jaminan deterministik (kontrak, bukan janji):** dua POST dengan body identik
> pada `generated_at` yang sama akan menghasilkan array `forecast` identik
> byte-per-byte. `generated_at` tentu berbeda — bandingkan `forecast[]`, bukan
> respons utuh.

---

### 5.2 `GET /api/v1/forecast/latest` — dipakai monitoring

**Query**

| Parameter | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `site_id` | string | Ya | Pola §3.2 |

Contoh: `GET /api/v1/forecast/latest?site_id=PLTS_KUPANG`

**Tidak ada parameter `horizon`.** Endpoint ini membaca forecast yang tersimpan
apa adanya (scheduler menghasilkan horizon 7 — lihat §5.1/§8); horizon
di-response = horizon yang memang tersimpan. Parameter tambahan yang tidak
dikenal (mis. `?horizon=24` dari template URL lama) **diabaikan, bukan
error** — response identik dengan panggilan tanpa parameter itu. Forecast
on-demand dengan horizon pilihan tetap lewat `POST /forecast` (§5.1).

**Respons: `200 OK`** — objek `ForecastSet` lengkap (§3.2) termasuk `forecast_id`.

```json
{
  "forecast_id": "6b1f0c2e-...",
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "resolution": "daily",
  "unit": "W/m2",
  "timezone": "Asia/Makassar",
  "ts": "2025-12-31T00:00:00+08:00",
  "generated_at": "2026-10-01T06:00:00+08:00",
  "horizon": 24,
  "model_version": "v1.0.0",
  "forecast": [ { "forecast_time": "...", "forecast_value": 212.284, "model_version": "v1.0.0" } ]
}
```

**Respons: `404`** — belum pernah ada forecast untuk `site_id`:
kode `E_NO_FORECAST` (bukan `E_NOT_FOUND` generik — monitoring perlu
membedakan "site tak dikenal" dari "site valid tapi belum ada forecast").
`site_id` tidak dikenal → `404` dengan kode `E_SITE_UNKNOWN`.

**Pemakaian monitoring:** panggil per interval, baca `generated_at`; jika
`generated_at` lebih tua dari SLA (mis. 90 menit untuk regenerasi per jam),
anggap layanan bermasalah — bukan memanggil `POST` on-demand.

---

### 5.3 `GET /api/v1/forecast/history` — riwayat tersimpan

**Query**

| Parameter | Tipe | Wajib | Default | Ketentuan |
|---|---|---|---|---|
| `site_id` | string | Ya | — | Pola §3.2 |
| `from` | RFC 3339 tz-aware | Tidak | — | Filter `generated_at >= from` |
| `to` | RFC 3339 tz-aware | Tidak | — | Filter `generated_at <= to`; `to < from` → `422` `E_TIME_RANGE_INVALID` |
| `limit` | integer | Tidak | `20` | 1..100; di luar → `422` `E_LIMIT_RANGE` |
| `cursor` | string | Tidak | — | Kursor opaque dari respons sebelumnya |
| `include_points` | boolean | Tidak | `false` | `true` = sisipkan array `forecast`; default **tidak** (ringan) |

**Respons: `200 OK`**

```json
{
  "items": [
    {
      "forecast_id": "6b1f0c2e-...",
      "site_id": "PLTS_KUPANG",
      "target": "irradiance",
      "resolution": "daily",
      "unit": "W/m2",
      "ts": "2025-12-31T00:00:00+08:00",
      "generated_at": "2026-10-01T06:00:00+08:00",
      "horizon": 24,
      "model_version": "v1.0.0",
      "forecast": "…hanya bila include_points=true…"
    }
  ],
  "page": {
    "limit": 20,
    "next_cursor": "eyJnIjoiMjAyNi0xMC0wMVQwNjowMDowMCJ9",
    "has_more": true
  }
}
```

**Aturan pagination (berlaku untuk semua endpoint collection di v1):**

1. **Cursor-based**, bukan offset — offset berubah isi halaman saat data baru masuk.
2. Cursor **opaque** (base64) — client tidak boleh mengurai/membangun sendiri.
3. Urutan: `generated_at` **descending**, tie-break `forecast_id` ascending (deterministik).
4. Halaman tidak boleh memuat `forecast_id` duplikat **dalam satu halaman**; lintas halaman bisa terulang bila data baru masuk di tengah — client **wajib dedupe** berdasarkan `forecast_id`.
5. `has_more=false` → `next_cursor` dihilangkan dari JSON (bukan `null`).
6. `from`/`to`/`site_id` **harus identik** antar halaman yang di-`follow`; cursor menyimpan kondisi filter — bila filter diubah di tengah → `422` `E_CURSOR_FILTER_MISMATCH`.
7. Default sort descending (terbaru dulu) = **kontrak**; mengubahnya = breaking.

**Respons `400`** untuk cursor rusak/kedaluwarsa: kode `E_CURSOR_INVALID`.

---

### 5.4 `GET /api/v1/model/info`

Tanpa parameter. **`200 OK`:**

```json
{
  "model_version": "v1.0.0",
  "target": "irradiance",
  "resolution": "daily",
  "algorithm": "ARIMA",
  "order": [1, 0, 2],
  "timezone": "Asia/Makassar",
  "history_min_rows": 30,
  "horizon_max": 30,
  "trained_at": "2026-10-01T08:00:00+08:00",
  "training_rows": 1096,
  "training_range": {
    "from": "2016-01-01T00:00:00+08:00",
    "to": "2026-01-31T00:00:00+08:00"
  },
  "features": [],
  "exog_supported": true,
  "deterministic": true,
  "engine": "forecast_engine.py",
  "preprocessing": "preprocessing.py"
}
```

| Field | Catatan |
|---|---|
| `algorithm`, `order` | `order` adalah array integer `[p,d,q]` — mesin-parsable, bukan string |
| `features` | Daftar fitur turunan; kosong `[]` bila polos |
| `training_rows` | Jumlah baris data yang benar-benar dipakai melatih artefak (sumber `model/artifacts.json`). Bukan baris di dataset mentah — untuk menilai apakah metrik berasal dari dataset yang cukup besar |
| `exog_supported` | Apakah model menerima variabel eksternal |
| `deterministic` | `true` = jaminan determinisme §5.1 — bila `false`, konsumen wajib diberi tahu (perubahan ini sendiri = **breaking**, karena membatalkan jaminan) |

Menambah field baru ke response ini = non-breaking (client harus mengabaikan field asing).

---

### 5.5 `GET /api/v1/model/performance`

**Query**

| Parameter | Tipe | Wajib | Default | Ketentuan |
|---|---|---|---|---|
| `site_id` | string | Ya | — | Pola §3.2 |
| `resolution` | string | Tidak | `daily` | `daily` \| `hourly` |

**`200 OK`:**

```json
{
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "resolution": "daily",
  "unit": "W/m2",
  "model_version": "v1.0.0",
  "evaluated_at": "2026-10-02T11:40:29+08:00",
  "evaluation_range": {
    "from": "2025-01-01T00:00:00+08:00",
    "to": "2025-12-31T00:00:00+08:00"
  },
  "n_samples": 336,
  "data_rows": 1096,
  "thresholds": {
    "max_mae": 40.0,
    "max_mape": 10.0,
    "max_mape_long": 15.0,
    "min_skill": 0.0,
    "primary_horizon": 7
  },
  "status": "NO_GO",
  "failures": [
    "MAPE horizon 7 = 25.56% > 10.00%",
    "MAPE horizon 30 = 24.39% > 15.00%"
  ],
  "horizons": [
    { "horizon": 1,  "mae": 40.513, "rmse": 50.400, "mape": 25.69, "skill": 0.291 },
    { "horizon": 7,  "mae": 40.255, "rmse": 50.022, "mape": 25.56, "skill": 0.244 },
    { "horizon": 14, "mae": 40.561, "rmse": 50.209, "mape": 25.69, "skill": 0.284 },
    { "horizon": 30, "mae": 39.750, "rmse": 48.954, "mape": 24.39, "skill": 0.235 }
  ]
}
```

| Field | Ketentuan |
|---|---|
| `evaluated_at` | **Waktu backtest DIBUAT** (dari `generated_at` laporan `report.json`), BUKAN waktu request. `null` bila belum pernah dievaluasi — jangan diisi waktu request, karena itu membuat "run terakhir" selalu tampak baru tiap halaman dibuka. Konsumen bisa memakainya sebagai "Last run" di dashboard monitoring |
| `data_rows` | Jumlah baris dataset yang dipakai backtest (`n_rows` laporan). **Berbeda dari `n_samples`** = jumlah origin forecast yang dinilai. Untuk menilai apakah metrik berasal dari dataset yang cukup besar |
| `mae`, `rmse` | Dalam `unit` respons; 3 desimal |
| `mape` | **Persen**, 2 desimal (mis. `25.56` = 25.56%) |
| `skill` | `1 − MAE_model/MAE_persistence`; bisa negatif (lebih buruk dari baseline) |
| `n_samples` | Jumlah pasangan actual–forecast yang benar-benar dipakai |
| `thresholds` | Ambang aktif — **echo** dari konfigurasi, agar konsumen bisa mengapa mengapa suatu titik dianggap tidak lulus |
| `status` | `"GO"` \| `"NO_GO"` — hasil `judge_backtest()`; `NO_GO` **bukan** error HTTP, tetapi `200 OK` (angka tetap valid dan berguna) |
| `failures` | Alasan `NO_GO`; kosong `[]` bila `GO` |
| `horizons` | Diurut naik; wajib memuat `horizon = 7` (primary) |
| Belum ada evaluasi | `200` dengan `horizons: []`, `status: "UNKNOWN"` — **bukan** `404` (metrik nol ≠ resource tidak ada) |

> Metrik pada contoh di atas adalah **angka nyata** dari backtest pada data
> repo (2025 holdout, 336 origin) — lihat
> [STRATEGI_EVALUASI.md](STRATEGI_EVALUASI.md) §5. Angka akan berubah bila
> dataset diganti (era5 vs solcast); strukturnya tetap. Metrik, ambang, dan
> aturan GO/NO-GO dikunci di dokumen strategi — bukan di endpoint ini.

---

### 5.6 `GET /api/v1/health` — tanpa API key

**`200 OK`:**

```json
{
  "status": "ok",
  "service_version": "v1.0.0",
  "timestamp": "2026-10-01T09:20:00+08:00",
  "uptime_seconds": 3725,
  "model_loaded": true
}
```

| `status` | HTTP | Arti |
|---|---|---|
| `ok` | `200` | Model termuat, endpoint siap |
| `degraded` | `200` | Layanan jalan tapi sebagian fungsi terganggu (mis. penyimpanan penuh) |
| `unavailable` | `503` | Model belum termuat / startup belum selesai; client **wajib retry** dengan `Retry-After` |

Field `model_version` sengaja **tidak** ada di health publik — detail model ada
di `/api/v1/model/info` yang butuh key (§1.1). `503` memakai envelope error
standar dengan kode `E_SERVICE_UNAVAILABLE`.

---

### 5.7 `GET /api/v1/forecast/actual` — aktual irradiance per tanggal

Endpoint tambahan (non-breaking §9.2) untuk panel engineer dan konsumen yang
memerlukan pencocokan aktual-vs-forecast per-titik. Forecast per-titik tetap
menjadi bagian dari `/forecast` response; endpoint ini menyediakan **actual**
agar kedua deret dapat digabung berdasarkan timestamp.

**Query**

| Parameter | Tipe | Wajib | Default | Ketentuan |
|---|---|---|---|---|
| `site_id` | string | Ya | — | Site terdaftar (§3.2); site tak dikenal → `404 E_SITE_UNKNOWN` |
| `from` | RFC 3339 tz-aware | Tidak | 60 hari sebelum `to` (atau sekarang, zona site) | Batas awal inklusif |
| `to` | RFC 3339 tz-aware | Tidak | Sekarang, zona site | Batas akhir inklusif; `to < from` → `422 E_TIME_RANGE_INVALID` |

Input `Z`, `+07:00`, `+08:00` diterima; sistem menormalisasi instan ke
UTC untuk perbandingan, respons Kupang diserialisasi sebagai `+08:00`
(§1.5). Timestamp naive → `422 E_TZ_REQUIRED` — zona tidak pernah ditebak.

**`200 OK` — satu titik per hari yang datanya tersedia:**

```json
{
  "site_id": "PLTS_KUPANG",
  "target": "irradiance",
  "unit": "W/m2",
  "timezone": "Asia/Makassar",
  "from": "2024-03-01T00:00:00+08:00",
  "to": "2024-03-07T00:00:00+08:00",
  "source": "data/actuals.csv",
  "points": [
    { "timestamp": "2024-03-01T00:00:00+08:00", "actual_value": 199.226 },
    { "timestamp": "2024-03-02T00:00:00+08:00", "actual_value": 212.604 }
  ],
  "n": 7
}
```

| Field | Tipe | Keterangan |
|---|---|---|
| `site_id` | string | Site yang diminta |
| `target` | literal `irradiance` | Satu-satunya actual yang tersedia di v1 |
| `unit` | literal `W/m2` | Rata-rata irradiance harian Solcast; bukan kW/kWh |
| `timezone` | literal `Asia/Makassar` | Zona site Kupang |
| `from`, `to` | RFC 3339 tz-aware | Rentang efektif, response memakai offset zona site |
| `source` | string | Label generik `data/actuals.csv`; tidak pernah mengembalikan path filesystem |
| `points[]` | array | Timestamp/value aktual yang tersedia, urut naik; hari kosong tidak diinterpolasi |
| `points[].timestamp` | RFC 3339 tz-aware | Timestamp tanggal aktual |
| `points[].actual_value` | number | Irradiance aktual W/m2, 3 desimal, `>= 0` |
| `n` | integer | Harus sama dengan `len(points)` |

**Data source & coverage:** `ghi_solcast_mean` dari B1 daily master, disiapkan
server-side oleh `python scripts/prepare_actuals.py` → `data/actuals.csv`.
Cakupan tersedia saat ini: **2023-01-01 – 2025-12-31**. Endpoint membalas
`200` + `points: []` + `n: 0` bila rentang valid tidak overlap — ini **bukan**
`404`, dan tidak berarti aktualnya nol. Bila file aktual belum terpasang/rusak,
`503 E_SERVICE_UNAVAILABLE` dengan pesan menjalankan script prepare.

Endpoint ini tidak membutuhkan model (`model_loaded`) karena hanya membaca
aktual historis; actual tetap tersedia saat model sedang dimuat ulang. Tetap
wajib `X-API-Key` (§1.1), dan hanya site terdaftar (§6.1).

**Pemakaian di grafik actual-vs-forecast:** cocokkan `forecast[].forecast_time`
dengan `points[].timestamp` berdasarkan instan UTC yang sama. Gunakan
**interseksi tepat** — tanpa interpolasi, tanpa nearest-date. Band visualisasi
`|actual_value - forecast_value|` adalah **error absolut per tanggal**,
bukan interval ketidakpastian model.

---

## 6. KODE ERROR

**Pembagian:** `4xx` = kesalahan client (jangan di-retry tanpa perbaikan), `5xx` = kesalahan layanan (boleh retry dengan backoff).

### 6.1 Kode aplikasi (field `error.code`)

| Kode | HTTP | Endpoint | Arti |
|---|---|---|---|
| `E_UNAUTHORIZED` | 401 | semua non-health | Header `X-API-Key` tidak ada |
| `E_FORBIDDEN` | 403 | semua non-health | Key salah / dicabut |
| `E_VALIDATION_FAILED` | 422 | POST, query | Body/query gagal validasi schema (bila `details` kosong) |
| `E_HORIZON_RANGE` | 422 | POST | `horizon` di luar 1..30 (daily) |
| `E_TZ_REQUIRED` | 422 | POST, history, actual | `timestamp`/`from`/`to` tanpa offset eksplisit (zone tidak boleh ditebak) |
| `E_HISTORY_TOO_SHORT` | 422 | POST | `history` < 30 item |
| `E_IRREGULAR_SERIES` | 422 | POST | Langkah waktu tidak seragam |
| `E_DUPLICATE_TIMESTAMP` | 422 | POST | Ada `timestamp` ganda |
| `E_VALUE_INVALID` | 422 | POST | `value` null / bukan angka / negatif |
| `E_TARGET_UNAVAILABLE` | 422 | POST | `target` ada di enum tapi belum aktif (§4) |
| `E_EXOG_INCOMPLETE` | 422 | POST | `exog` tidak menutup jendela forecast |
| `E_EXOG_MISSING_COLUMN` | 422 | POST | Kolom exog yang diminta tidak ada di data |
| `E_TIME_RANGE_INVALID` | 422 | history, actual | `to < from` |
| `E_LIMIT_RANGE` | 422 | history | `limit` di luar 1..100 |
| `E_CURSOR_INVALID` | 400 | history | Cursor rusak / kedaluwarsa / bukan milik service ini |
| `E_CURSOR_FILTER_MISMATCH` | 422 | history | Filter berubah di tengah paginasi |
| `E_IDEMPOTENCY_MISMATCH` | 409 | POST | `Idempotency-Key` sama, body berbeda |
| `E_SITE_UNKNOWN` | 404 | latest, history, performance, actual | `site_id` tidak dikenal service |
| `E_NO_FORECAST` | 404 | latest | Site valid, forecast belum pernah dibuat |
| `E_RATE_LIMITED` | 429 | semua non-health | Kuota 60 req/menit terlampaui |
| `E_INTERNAL` | 500 | semua | Galat tak terduga; `message` opsional, **jangan** bocorkan stack trace |
| `E_SERVICE_UNAVAILABLE` | 503 | health, actual & lainnya | Model/loading tidak siap, atau file actual data tidak terpasang/rusak |
| `E_MODEL_FAILED` | 500 | POST | Model ARIMA gagal konvergen atas input valid |

### 6.2 Kesalahan HTTP polos

| Kode | Pemicu |
|---|---|
| `400` | JSON tidak valid / Content-Type salah → envelope `E_VALIDATION_FAILED` |
| `404` | Path tidak dikenal → envelope `E_NOT_FOUND` |
| `405` | Method tidak diizinkan, sertakan header `Allow` |
| `415` | Content-Type selain `application/json` |
| `500` | Bug server → `E_INTERNAL` |

**Ketentuan umum:** setiap respons error **wajib** memuat envelope §3.3 (bukan
stack trace, bukan HTML). `422` **wajib** mengisi `details[]` per field.
`2xx` tidak pernah memuat objek `error`.

---

## 7. CONTOH ALUR MONITORING

```http
# 1. Cek layanan (tanpa key, boleh sering)
GET /api/v1/health

# 2. Ambil forecast terbaru (per interval, mis. tiap 15 menit)
GET /api/v1/forecast/latest?site_id=PLTS_KUPANG
X-API-Key: ...

# 3. Bandingkan actual vs forecast di sisi monitoring; refresh metrik mingguan
GET /api/v1/model/performance?site_id=PLTS_KUPANG
X-API-Key: ...

# 4. Penelusuran (mis. investigasi insiden)
GET /api/v1/forecast/history?site_id=PLTS_KUPANG&from=2026-09-01T00:00:00+08:00&limit=50
X-API-Key: ...
→ 200, has_more=true, next_cursor="eyJn..."
GET /api/v1/forecast/history?site_id=PLTS_KUPANG&from=2026-09-01T00:00:00+08:00&limit=50&cursor=eyJn...
X-API-Key: ...
```

---

## 8. SUMBER DATA ENDPOINT (implementasi)

| Endpoint | Sumber |
|---|---|
| `POST /forecast` | `preprocessing.prepare_dataset()` → `forecast_engine.generate_forecast()` — **tanpa penyimpanan** |
| `GET /forecast/latest`, `/forecast/history` | Hasil yang disimpan scheduler latar belakang (menjalankan rantai yang **identik**) |
| `GET /model/info` | Konstanta `forecast_engine.MODEL_*` + manifest model |
| `GET /model/performance` | Hasil pipeline evaluasi (walk-forward pada data uji), bukan angka manual |
| `GET /health` | Status proses + flag pemuatan model |

---

## 9. ATURAN BREAKING CHANGE

### 9.1 Breaking (butuh `/api/v2` atau persetujuan ulang)

- Menghapus/mengganti nama field yang sudah ada
- Mengubah tipe field (mis. number → string)
- Mengubah `unit` pada `target` yang sudah dipublikasikan
- Mengubah satuan/timestamp format (mis. `+08:00` → UTC Z)
- Mengecilkan `horizon` maksimum, atau mengubah arti satu step
- Mengubah makna/menghapus kode error, atau HTTP status untuk kondisi yang sama
- Mengubah urutan default atau semantik cursor pagination
- Mengubah default nilai query (`limit` default 20 → 50 = behavior change)
- Menurunkan `deterministic` di `/model/info` dari `true` → `false`
- Menjadikan field opsional menjadi wajib di request, atau menghapus field request

### 9.2 Non-breaking (boleh tanpa versi baru)

- Menambah field **opsional** baru pada respons (client wajib mengabaikan field asing)
- Menambah endpoint baru di `/api/v1`
- Menambah nilai enum baru pada `target` / `resolution` (client wajib toleran nilai asing)
- Menambah query parameter opsional dengan default yang mempertahankan perilaku lama
- Menambah item baru pada `horizons[]` performance
- Memperbaiki typo `message` (bukan `code`)
- Perbaikan bug yang mengembalikan perilaku **sesuai** kontrak ini

### 9.3 Prosedur

1. Perubahan apapun → edit dokumen ini + tulis baris di §11 "Riwayat Revisi".
2. Jika §9.1 terpicu → **stop**: naikkan versi kontrak ke v2 dan pertahankan `/api/v1` lama selama periode transisi.
3. Contract test (Fase 5) gagal otomatis bila respons menyimpang dari dokumen ini.

---

## 10. GATE — WAJIB DISEPAKATI SEBELUM FREEZE v1.0.0

Kontrak ini **tidak dianggap final** sampai tiga hal berikut diputuskan.
Semuanya sengaja dibiarkan terbuka agar keputusan terburu-buru tidak
menjadi breaking change nanti.

| # | Gate | Pilihan | Dampak ke kontrak |
|---|---|---|---|
| 10.1 | Target yang dipublikasikan | Saat ini **`irradiance` saja** | Menambah `energy_kwh`/`power_kw` = non-breaking; mengubah `unit` `irradiance` = breaking |
| 10.2 | Sumber `power_history` | Opsi A (kWh harian) / B (kW turunan) / C (data inverter) — spesifikasi §4.2 | Menentukan `target` mana yang aktif dan unit final |
| 10.3 | Kalibrasi irradiance → energi | Faktor `kapasitas × PR` — spesifikasi §4.4 | Prasyarat mengaktifkan `energy_kwh`/`power_kw` |
| 10.4 | Zona waktu | **`Asia/Makassar`** — sudah diverifikasi dari lokasi PLTS (spesifikasi §4.1) | Mengganti zona setelah freeze = breaking |
| 10.5 | Retensi & jadwal scheduler | Jadwal **sudah diimplementasikan** (Fase 4): regenerasi per jam (3600s), tick pertama saat startup, env `SCHEDULER_ENABLED`/`SCHEDULER_INTERVAL_SECONDS`; history mengikuti store in-memory (hilang saat restart). Retensi berbasis DB masih terbuka | Memengaruhi SLA `generated_at` yang dipantau monitoring |
| 10.6 | Rate limit & key management | 60 req/menit (§1.3) — disetujui konsumen? | Angka rate limit dinaikkan = non-breaking; diturunkan = breaking |

**Checklist freeze:**

- [ ] §10.1 target & unit final
- [ ] §10.2 sumber daya final
- [ ] §10.3 kalibrasi final
- [ ] §10.4 zona waktu disetujui konsumen
- [ ] §10.5 retensi & jadwal scheduler
- [ ] §10.6 rate limit disetujui konsumen
- [ ] Contoh respons §5 dijalankan sungguhan dan hasilnya disalin ke dokumen ini (angka placeholder diganti)
- [ ] Contract test Fase 5 dibuat **dari** dokumen ini
- [ ] Versi ditandai `v1.0.0` FINAL di tabel atas

---

## 11. RIWAYAT REVISI

| Versi | Tanggal | Perubahan |
|---|---|---|
| v1.0.0-draft | 2026-10-15 | **API key per konsumen (§1.1) + kuota terlihat (§1.3)** — format env `API_KEYS=label:key` per konsumen: label = identitas untuk log pemakaian server (`consumer=<label>` per request ber-key; nilai key tidak pernah dicatat) dan pencabutan per konsumen (hapus satu entri → deploy → `403`, konsumen lain tak terpengaruh). Entri tanpa label tetap sah (label `legacy`) — kompatibilitas mundur. **Tidak ada perubahan HTTP untuk klien**: header tetap `<key>` tanpa prefix, verifikasi tetap lookup env tanpa fallback; label = metadata deployment (bukan kontrak). §1.3: header `X-RateLimit-Limit`/`-Remaining` kini juga di respons **sukses** (sebelumnya hanya 429), dan `X-RateLimit-Reset` di 429 yang **dijanjikan kontrak sejak draft pertama tapi belum pernah dikirim** kini benar-benar ada (perbaikan menuju kontrak, §9.2). Semua **non-breaking** (§9.2: menambah header + dokumentasi + metadata; field/kode/status tidak berubah). |
| v1.0.0-draft | 2026-10-15 | **Perbaikan filter `GET /forecast/history` (§5.3)** — parameter `from` sebelumnya TIDAK PERNAH diterapkan karena route memakai model Pydantic via `Depends()` dengan alias `from`; FastAPI mencocokkan nama field (`from_`), bukan alias, sehingga `?from=...` diam-diam diabaikan dan filter grafik historis selalu mengembalikan semua data. Route kini memakai `Query(alias="from")` eksplisit (pola identik §5.7). Efek: `to < from` kini benar-benar `422 E_TIME_RANGE_INVALID` (sebelumnya 200 kosong), filter `from`/`to` menyaring `generated_at` sesuai spesifikasi, `from` naive → `422 E_TZ_REQUIRED` (baris §6.1 `E_TZ_REQUIRED` diperluas ke history), dan `limit` di luar 1..100 kini `422 E_LIMIT_RANGE` (kode §6.1; sebelumnya `E_VALIDATION_FAILED` generik — kode spesifik diwajibkan kontrak, jadi perbaikan menuju kontrak, bukan perubahan). **§5.2 diperjelas**: `latest` TIDAK punya parameter `horizon`; membaca forecast tersimpan apa adanya (scheduler horizon 7); parameter tak dikenal diabaikan, bukan error (pola "query tak dikenal diabaikan" konsisten dengan HTTP). Model query `ForecastLatestQuery`/`ForecastHistoryQuery` dihapus (yatim; sumber bug alias). Semua **non-breaking** (§9.2): response field tidak berubah; perilaku yang berubah semuanya menuju spesifikasi yang sudah tertulis. |
| v1.0.0-draft | 2026-10-14 | **Scheduler latar belakang (Fase 4) diimplementasikan** — §5.1 menyatakan penyimpanan ada di scheduler; kini job itu ada (`app/scheduler.py`): tick pertama saat startup lalu regenerasi per jam (§1.4 / SLA §5.2), memanggil rantai **identik** `service.run_forecast` (§8), history dari tail `data/actuals.csv`. **Gate §10.5** diperbarui: interval sudah diputuskan (3600s; env `SCHEDULER_ENABLED`, `SCHEDULER_INTERVAL_SECONDS`); retensi lintas-restart masih terbuka. Perilaku kontrak tidak berubah — `GET /forecast/latest` kini benar-benar terisi 200 setelah startup (sebelumnya selalu 404 `E_NO_FORECAST` karena scheduler belum pernah dibangun). Tidak ada perubahan field/kode; **non-breaking** (§9.2). |
| v1.0.0-draft | 2026-10-12 | **§5.5 `evaluated_at` dikoreksi**: kini memakai `generated_at` laporan backtest (waktu asli pembuatan), bukan `site_now()` (waktu request) — sebelumnya membuat "run terakhir" selalu tampak baru tiap request. Nilai `null` bila belum pernah dievaluasi. **Tambah `data_rows`** (§5.5) = `n_rows` laporan, dibedakan dari `n_samples`. **§5.4 tambah `training_rows`** dari `model/artifacts.json`. Semua penambahan field = **non-breaking** (§9.2); koreksi `evaluated_at` = perbaikan kontrak menuju spesifikasi §1.5 (respons sebelumnya bisa mengirim timestamp naive dari laporan lama — parser kini melokalisasi eksplisit ke zona site). |
| v1.0.0-draft | 2026-10-08 | Tambah `GET /api/v1/forecast/actual` §5.7 sebagai endpoint additive/non-breaking (§9.2): actual W/m2 per titik, timestamp strict, empty range = 200 `points:[]`; data disajikan melalui API, bukan browser CSV. Kode error §6.1 `E_TZ_REQUIRED`/`E_TIME_RANGE_INVALID`/`E_SITE_UNKNOWN`/`E_SERVICE_UNAVAILABLE` diperluas ke actual. |
| v1.0.0-draft | 2026-10-02 | **Sumber kebenaran versi: `model/VERSION` (baris pertama), dibaca `app/version.py`; tidak ada literal versi di kode.** Skema semver dilonggarkan ke `^v\d+\.\d+(\.\d+)?$` agar `v1.0` dari file itu diterima — **non-breaking** (§9.2, melebarkan yang diterima) |
| v1.0.0-draft | 2026-10-02 | §1.5 aturan zona tunggal (UTC internal, serialisasi zona site); §5.5 performance diperkaya `skill`/`thresholds`/`status`/`failures` dan diisi angka nyata backtest — semua penambahan field respons = **non-breaking** (§9.2); sumber metrik/ambang dikunci di [STRATEGI_EVALUASI.md](STRATEGI_EVALUASI.md) |
| v1.0.0-draft | 2026-10-01 | Draft pertama: 6 endpoint, 24 kode error (§6.1–6.2), pagination cursor, satuan terkunci per target, 6 gate freeze |
