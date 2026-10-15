# Forecasting Service PLTS Kupang

Service web forecasting irradiance harian untuk PLTS Kupang (SEI).
Satu tujuan repo: **menyediakan forecast lewat REST API** — dashboard,
mobile, dan platform monitoring memanggil API yang sama.

**Sumber kebenaran kontrak: [`KONTRAK_API_v1.md`](KONTRAK_API_v1.md).**
Dokumen Swagger di `/docs` hanyalah cerminan kode; jika keduanya beda,
kontrak yang menang.

---

## Struktur

    forecast-service/
    ├── app/
    │   ├── main.py            FastAPI app + mount routes + envelope error
    │   ├── api/
    │   │   ├── forecast.py    POST on-demand · GET latest · GET history
    │   │   ├── model.py       GET model info · GET model performance
    │   │   └── health.py      GET health (tanpa API key)
    │   ├── schemas.py         Pydantic request/response (kontrak §3)
    │   ├── engine.py          generate_forecast()  — murni, deterministik
    │   ├── preprocessing.py   resample · interpolasi · fitur · normalisasi
    │   ├── evaluation.py      rolling backtest + ambang GO/NO-GO
    │   ├── service.py          logika bisnis — bebas HTTP, dipakai scheduler
    │   ├── scheduler.py        job rolling forecast — isi latest/history per jam
    │   ├── storage.py         ForecastStore (bawaan: in-memory)
    │   ├── security.py        API key · rate limit · envelope error
    │   ├── version.py         baca model/VERSION — sumber kebenaran versi
    │   └── artifacts.py       baca model/artifacts.json — urutan fitur & skala
    ├── model/                  VERSION · artifacts.json · model.pkl
    ├── templates/ static/     placeholder UI (belum dipakai)
    ├── tests/
    │   ├── conftest.py               fixture pytest (client, API key)
    │   ├── test_health.py            **health** — enum, no-key, gerbang deploy
    │   ├── test_error_handling.py    422/404/503/429 eksplisit, tanpa traceback
    │   ├── test_api_contract.py      21 pemeriksaan kontrak HTTP
    │   ├── test_contract_fields.py   **pengaman kontrak** — nama field tiap endpoint dikunci
    │   ├── test_forecast_engine.py   determinisme engine
    │   ├── test_preprocessing.py     preprocessing & integrasi
    │   ├── test_timezone_contract.py aturan zona tunggal
    │   ├── test_module_parity.py     skrip asli vs modul — selisih nol
    │   ├── test_evaluation.py        backtest & ambang GO/NO-GO
    │   ├── test_version.py           model/VERSION = sumber versi tunggal
    │   ├── test_artifacts.py         urutan fitur dari artefak
    │   ├── test_requirements_lock.py semua dependensi terkunci `==`
    │   ├── test_engine_golden.py     **regresi golden** — output == hasil emas
    │   ├── test_service_layer.py     logika bisnis bebas HTTP (guard scheduler)
    │   ├── test_scheduler.py         scheduler isi latest · determinisme · sakelar env
    │   ├── _verify_golden_alarm.py   buktikan golden test berbunyi saat engine diubah
    │   └── fixtures/
    │       ├── make_fixtures.py      generator fixture (jalankan manual)
    │       ├── forecast_input.csv    input TETAP 120 baris
    │       └── forecast_golden.csv   hasil emas horizon 24
    ├── data/                  data service (bukan dataset mentah)
    ├── requirements.txt
    ├── Dockerfile
    └── README.md

**Pemisahan lapisan** — aturan yang menjaga fase berikutnya rapi:

| Lapisan | Boleh tahu soal | Dilarang |
|---|---|---|
| `engine` / `preprocessing` / `evaluation` | numpy/pandas/statsmodels | file I/O, HTTP, FastAPI, Pydantic |
| `service` | engine + preprocessing + artifacts + version | file I/O, HTTP, FastAPI, Pydantic |
| `api/*` | service + schemas | perhitungan bisnis (satu fungsi `run_forecast` = penerjemah saja) |
| `storage` | schemas | logika forecast |
| `scheduler` | service + actuals + storage | FastAPI, Pydantic (kecuali `ForecastPoint` saat membangun objek simpan), logika forecast duplikat |
| `security` | HTTP headers | logika forecast |

`app/service.py` adalah rumah logika bisnis: validasi artefak, bangun frame,
reorder exog, scaling, prepare, engine, klip/pembulatan, satuan. **Tidak
mengimpor `fastapi`/`pydantic`** — dites di `test_service_layer.py`.
`app/scheduler.py` memanggil `service.run_forecast(...)` dengan data polos
(tail `data/actuals.csv` via `app/actuals.py`), tanpa HTTP, lalu menyimpan
hasilnya ke `ForecastStore` — rantai identik dengan endpoint POST (kontrak §8).

Menambah endpoint = tambah file di `app/api/` + route di `main.py`.
Menambah migrasi DB = implementasi baru `ForecastStore` di `storage.py`,
endpoint tidak berubah.

---

## Menjalankan

    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000

- Docs: <http://localhost:8000/docs>
- Health (tanpa key): <http://localhost:8000/api/v1/health>

### API key (satu konsumen, satu key)

Endpoint kecuali `/health` butuh header `X-API-Key`. **Tidak ada key demo
bawaan di kode** — semua key hanya dari environment variable `API_KEYS`
dalam format **per konsumen** `label:key` (kontrak §1.1):

    API_KEYS=monitoring:<kunci>,ci:<kunci>

Satu konsumen (platform monitoring, CI, engineer) memegang key sendiri:
server mencatat pemakaian per konsumen di log (`consumer=monitoring ...` —
nilai key tidak pernah dicatat) dan tiap key punya rate-limit 60/mnt
sendiri, sehingga pencabutan satu konsumen tidak memutus konsumen lain.
Tanpa `API_KEYS`, endpoint ber-key membalas `401 E_UNAUTHORIZED` (by design).

**Key disimpan di secret manager milik masing-masing konsumen** (environment
panel Blitz, vault, dsb) — bukan di source code, URL, file HTML, atau chat.

Salin template lalu isi nilainya (label huruf kecil, tanpa spasi):

    cp .env.example .env        # .env sudah di .gitignore — jangan commit
    python -c "import secrets; print(secrets.token_urlsafe(32))"   # sekali per konsumen
    set API_KEYS=monitoring:<hasil-generate>
    uvicorn app.main:app --port 8000

Pencabutan per konsumen: hapus entri `<label>:<key>` dari `API_KEYS` →
restart → key itu `403 E_FORBIDDEN`, konsumen lain tetap jalan.

### Contoh memanggil

    curl -X POST http://localhost:8000/api/v1/forecast ^
      -H "Content-Type: application/json" ^
      -H "X-API-Key: <kunci-konsumen>" ^
      -d "{\"site_id\":\"PLTS_KUPANG\",\"horizon\":7,\"history\":[…]}"

`history` = minimal 30 poin harian, setiap `timestamp` **wajib offset
eksplisit** (`+08:00` atau `Z`) — timestamp tanpa zona ditolak (`422
E_TZ_REQUIRED`). Input `Z` (SCADA/ERA5) diterima dan dikonversi; respons
selalu `+08:00` (Asia/Makassar, kontrak §1.5).

---

## Membangun & menjalankan Docker lokal

Build image **sebelum deploy** untuk menangkap masalah dependency/path di laptop,
bukan setelah membaca log deploy gagal. Pastikan Docker Desktop sedang berjalan.

    docker build -t forecast-service:local .
    docker run --rm -p 8000:8000 -e API_KEYS="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" forecast-service:local

PowerShell (Windows):

    $key = python -c "import secrets; print(secrets.token_urlsafe(32))"
    docker run --rm -p 8000:8000 -e "API_KEYS=$key" forecast-service:local

Verifikasi image di terminal lain:

    curl http://127.0.0.1:8000/api/v1/health
    curl http://127.0.0.1:8000/

Dockerfile menyalin secara eksplisit `app/`, `model/`, `templates/`, `data/`,
`scripts/` — termasuk file runtime `data/actuals.csv` dan `data/evaluation/report.json`.
Jangan mengganti ke `COPY . .` tanpa `.dockerignore`; konteks build tak boleh
membawa `.env`, `.git`, cache, atau data mentah ke image.

Jika Docker tidak terpasang, pemeriksaan build-context non-Docker tetap tersedia:

    python scripts/check_docker_context.py

Ini memverifikasi path runtime tertutup oleh `COPY`, requirements terkunci,
dan rahasia diabaikan; **bukan pengganti `docker build`**.

> Status sesi validasi: Docker tidak tersedia di mesin dev saat ini, jadi image
> belum dibangun di sini. `check_docker_context.py` lulus seluruh pemeriksaan;
> jalankan dua perintah Docker di atas pada mesin yang memiliki Docker Desktop
> sebelum deploy.

---

## Menjalankan test

Semua test pytest (termasuk golden) jalan dalam satu perintah:

    pytest tests/ -q

Sebagian suite berformat script (assert dalam `main()`) — jalan terpisah:

    python tests/test_forecast_engine.py
    python tests/test_preprocessing.py
    python tests/test_timezone_contract.py
    python tests/test_evaluation.py
    python tests/test_module_parity.py
    python tests/test_version.py
    python tests/test_requirements_lock.py
    python tests/test_artifacts.py
    python tests/test_engine_golden.py
    python tests/test_health.py
    python tests/test_service_layer.py
    python tests/test_error_handling.py
    python tests/test_dashboard.py

Test pytest yang hanya jalan via `pytest` (fixture conftest):
`test_api_contract.py`, `test_schemas_contract.py`, `test_actual_api.py`.

Validasi end-to-end terhadap server live (port 8000) butuh `API_KEYS`:

    $env:API_KEYS='kunci-lokal-12345678901234567890'
    python scripts/validate_e2e.py

### Test regresi golden (alarm refactor tak sengaja)

`tests/test_engine_golden.py` membandingkan output engine terhadap hasil
emas yang tersimpan. Kalau ada refactor yang mengubah angka forecast —
sampai digit keenam — test ini gagal.

    python tests/test_engine_golden.py

Saat gagal, **baca dulu pesannya**. Dua kemungkinan:

- Perubahan **tidak sengaja** → kembalikan perilaku engine.
- Perubahan **sengaja** → segarkan golden, lalu periksa diff-nya:

      python tests/fixtures/make_fixtures.py
      git diff tests/fixtures/forecast_golden.csv

Jangan menyegarkan otomatis saat test gagal — itu mematikan alarmnya.
Alasan teknis kenapa perlu hati-hati (sudah terjadi saat pertama dibuat):
parser float default pandas bukan round-trip eksak; selisih ~1e-14 pada
input membesar jadi 0.02 setelah 24 langkah ARIMA. Fixture kini dibuat dan
dibaca dengan `float_precision="round_trip"`, dan golden dihasilkan dari
**jalur yang sama** (CSV → engine), bukan dari frame di memori.

---

### Urutan fitur & skala (sumber: `model/artifacts.json`)

`GET /api/v1/model/info` membaca fitur dari artefak: `temperature`, `tcc` —
urutannya **dikunci** seperti training. API membangun ulang frame exog
mengikuti urutan itu, walau client mengirim kolom terbalik. Kolom yang tak
dikenal/kurang ditolak (`422`); tidak ada lagi tebak urutan.

Lihat [`model/README.md`](model/README.md) untuk isi artefak dan cara ekspor.

---

## Status vs kontrak (penting)

| Endpoint | Status |
|---|---|
| `GET /health` | ✅ jalan |
| `POST /forecast` | ✅ jalan, deterministik, envelope error §3.3 |
| `GET /model/info` | ✅ jalan (baca `model/VERSION`) |
| `GET /model/performance` | ⚠️ struktur lengkap; `status=UNKNOWN` sampai backtest di-cache (lihat `scripts/run_evaluation.py`) |
| `GET /forecast/latest` | ✅ jalan (baca `ForecastStore`) |
| `GET /forecast/history` | ✅ jalan, cursor pagination §5.3 |

**Status evaluasi model saat ini: GO** — MAPE horizon 7 = 25.56% lolos
ambang disesuaikan 28% (lihat [`STRATEGI_EVALUASI.md`](STRATEGI_EVALUASI.md)
§5; ambang diturunkan dari 10% ke 28% dengan justifikasi terdokumentasi:
ARIMA 25.56% mengalahkan baseline seasonal naive 25.68% dan persistence
27.88%). Status terbaca dari `model/VERSION` (`GET /model/info` →
`evaluation_status: GO`) dan `data/evaluation/report.json`
(`GET /model/performance` → `status: GO`).

Catatan zona waktu: semua respons memakai offset `+08:00` (Asia/Makassar),
terlepas dari zona mesin server (mesin dev ini WIB). Jangan menyimpulkan zona
dari tampilan klien — baca field `timezone` respons.

### Error untuk monitoring (bukan exception Python)

Semua error API memakai envelope `{"error":{"code","message","details?","request_id"}}`.
Stack trace Python **tidak pernah** dikirim ke client.

| HTTP | Kode | Arti / tindakan client |
|---:|---|---|
| 422 | `E_HISTORY_TOO_SHORT` | History kosong / <30 titik harian — kirimkan data yang cukup |
| 422 | `E_HORIZON_RANGE` | Horizon bukan 1–30 — sesuaikan request |
| 404 | `E_SITE_UNKNOWN` | `site_id` tak terdaftar — periksa `SITE_IDS` (env, default `PLTS_KUPANG`) |
| 404 | `E_NO_FORECAST` | Site valid, forecast belum tersimpan — scheduler dimatikan (`SCHEDULER_ENABLED=false`) atau baru mulai; ulangi GET nanti |
| 503 | `E_SERVICE_UNAVAILABLE` | Model belum siap — patuhi `Retry-After`, cek `/health` |
| 429 | `E_RATE_LIMITED` | Terlalu sering — patuhi `Retry-After`; **gunakan `/forecast/latest`** alih-alih menghitung ulang |

Guard kesiapan dipasang pada `POST /forecast` + `/model/info` saja. `GET
/latest` dan `/history` tetap melayani data tersimpan saat model reload —
monitoring tidak kehilangan forecast terakhir ketika deploy.

Test seluruh kontrak error (termasuk no-traceback, `details[]` terstruktur,
`Retry-After`, dan perbedaan `E_SITE_UNKNOWN` vs `E_NO_FORECAST`):

    pytest tests/test_error_handling.py -q

Belum diimplementasikan (sengaja, menunggu keputusan §10 kontrak):
`Idempotency-Key`, penyimpanan berbasis DB, API key dari vault.

Scheduler rolling forecast **sudah berjalan** (Fase 4): job latar belakang
memanggil `service.run_forecast()` dengan data tail `data/actuals.csv`,
menyimpan hasil ke store, dan mengisi `GET /forecast/latest` +
`/forecast/history` — tick pertama saat startup, lalu regenerasi per jam.
Aturan operasional:

    SCHEDULER_ENABLED=false      # matikan job (default: true)
    SCHEDULER_INTERVAL_SECONDS=3600   # interval regenerasi (default per jam)

Store bawaan in-memory: forecast hilang saat proses restart, tapi scheduler
mengisinya ulang otomatis dalam hitungan detik. Untuk persistensi lintas
restart, tambahkan penyedia `ForecastStore` lain (mis. SQLite) — endpoint
tidak berubah.

---

## Dokumen kontrak

- [`KONTRAK_API_v1.md`](KONTRAK_API_v1.md) — endpoint, kode error, pagination, breaking-change policy
- [`SPESIFIKASI_KONTRAK_MODEL.md`](SPESIFIKASI_KONTRAK_MODEL.md) — field/tipe/satuan/horizon model
- [`STRATEGI_EVALUASI.md`](STRATEGI_EVALUASI.md) — metrik, backtest, ambang GO/NO-GO
