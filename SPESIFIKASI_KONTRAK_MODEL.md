# SPESIFIKASI KONTRAK INPUT & OUTPUT MODEL
### Forecasting Service PLTS Kupang — SEI

| | |
|---|---|
| Dokumen | `SPESIFIKASI_KONTRAK_MODEL.md` |
| Versi | 1.0 (draft) |
| Tanggal | 2026-10-01 |
| Fungsi | Dasar pembuatan skema Pydantic (Fase 1) dan test kontrak (Fase 5) |
| Sumber nilai | Data aktual pada folder `Data-20261001T082404Z-1-001` |

> **Aturan dokumen ini:** setiap nama field, tipe, satuan, dan batas di bawah ini adalah kontrak. Perubahan setelah Fase 1 = perubahan kontrak = wajib naik versi `model_version`.

---

## 0. Keputusan Resolusi

Model tidak boleh melayani tiga resolusi sekaligus dalam satu endpoint. Pilih **satu** resolusi utama:

| Resolusi | Ketersediaan data | Status model di notebook | Rekomendasi |
|---|---|---|---|
| **Harian (1x sehari)** | Lengkap 2016–2026 | ARIMA tervalidasi, MAPE 6.44–7.50% | **Utama — dipakai untuk PoC** |
| Per jam (07:00–17:00) | CAMS hourly terbatas (Mar 2024, Des 2025–Jan 2026) | SARIMA MAPE 9.6–28.3%, belum matang | Fase 2 |
| 5 menit | Solcast 315.360 baris (2023–2025) | Tidak ada model | Sumber input, bukan target |

Spesifikasi di bawah didefinisikan untuk **resolusi harian** sebagai kontrak v1. Varian hourly dicantumkan di Bagian 6.

---

## 1. KONTRAK INPUT

### 1.1 Field wajib

| # | Nama field | Tipe | Satuan | Rentang wajar (validasi) | Rentang teramati pada data | Catatan |
|---|---|---|---|---|---|---|
| 1 | `site_id` | `str` | — | `^[A-Z]{2,4}[_A-Z0-9]{2,12}$` | `PLTS_KUPANG` | Identifier statis per site. Pola di atas mengikuti konvensi `PLTS_KUPANG` / `PLTS001` |
| 2 | `ts` | `datetime` (tz-aware) | — | Wajib `tzinfo` tidak `None` | — | **Zona: `Asia/Makassar` (WITA, UTC+8)** — lihat §4.1 |
| 3 | `irradiance` | `float` | W/m² | `0 ≤ x ≤ 1500` | 5-menit: 0–1015 · harian-rata: 34.66–299.71 | `null` = `NaN`/`0` tidak boleh diizinkan tanpa penanda |
| 4 | `temperature` | `float` | °C | `-10 ≤ x ≤ 50` | 5-menit: 22–36 · harian-rata: 23.36–29.42 | Suhu udara dekat permukaan |
| 5 | `power_history` | `list[float]` | kW | `0 ≤ x ≤ kapasitas_plant` | **Tidak ada data** — lihat §4.2 | Panjang daftar = `history_len` (lihat §1.3) |
| 6 | `horizon` | `int` | step | `1 ≤ x ≤ 30` (harian) | notebook: 1, 7, 30 | Satu step = satu baris output |

### 1.2 Field opsional (disarankan, tidak mengikat)

| Nama field | Tipe | Satuan | Rentang wajar | Alasan |
|---|---|---|---|---|
| `ghi_history` | `list[float]` \| `null` | W/m² | `0–1500` | Sumber irradiance alternatif bila power_history tidak tersedia |
| `cloud_cover` | `float` \| `null` | % | `0–100` | Tersedia sebagai `tcc` pada `B1_daily_master_2016_2026.csv` |
| `precipitation_mm` | `float` \| `null` | mm | `0–500` | Tersedia; maks teramati 143.49 |
| `resolution` | `enum` | — | `daily` \| `hourly` | Default `daily`; menyediakan jalur ke varian hourly |

### 1.3 Bentuk `power_history`

```
power_history : list[float]
  panjang      : history_len (harus ≥ 1)
  panjang seragam untuk semua request dalam satu endpoint
  urutan       : kronologis, terbaru di index terakhir
  penyusun     : kWh per step ÷ durasi step (lihat §4.2)
  nilai        : 0 ≤ x ≤ kapasitas_plant, NaN tidak diizinkan
```

---

## 2. KONTRAK OUTPUT

### 2.1 Satu baris forecast

| # | Nama field | Tipe | Satuan | Rentang wajar | Catatan |
|---|---|---|---|---|---|
| 1 | `forecast_time` | `datetime` (tz-aware) | — | Wajib tz-aware, **zona sama dengan `ts`** | Waktu target forecast, bukan waktu dibuat |
| 2 | `forecast_value` | `float` | mengikuti `target` | `0 ≤ x ≤ batas atas` (mis. irradiance ≤ 1500) | Dibulatkan 3 desimal sebelum dikirim |
| 3 | `model_version` | `str` | — | Semver: `^v?\d+\.\d+(\.\d+)?$` — dibaca dari `model/VERSION` (sumber tunggal) | Contoh `v1.0` |

> **Selaras dengan kontrak HTTP:** nama dan tipe field di atas sudah dikunci di
> [KONTRAK_API_v1.md](KONTRAK_API_v1.md) §3.1. Jika ingin mengubahnya, ubah
> kedua dokumen sekaligus dan jalankan §9 (breaking change).

> **Catatan implementasi (Fase 1, lihat §8):** mesin yang sudah dibuat
> (`forecast_engine.generate_forecast`) memprediksi kolom `target` apa pun
> yang diberikan — default `irradiance` (W/m²). Field keluarannya bernama
> **`forecast_value`**, bukan `forecast_kw`, karena satuan mengikuti `target`.
> Pemetaan `forecast_value` → `forecast_kw` (atau `forecast_kwh`) terjadi di
> lapisan API **setelah** §4.2 dan §4.4 diputuskan. Sampai saat itu, `unit`
> pada respons harus ikut diisi sesuai `target`, jangan dikunci `kW`.


### 2.2 Respons endpoint (daftar langkah horizon)

```json
{
  "site_id": "PLTS_KUPANG",
  "ts": "2026-10-01T00:00:00+08:00",
  "generated_at": "2026-10-01T00:05:12+08:00",
  "resolution": "daily",
  "horizon": 7,
  "unit": "W/m2",
  "timezone": "Asia/Makassar",
  "model_version": "v1.0.0",
  "forecast": [
    {
      "forecast_time": "2026-10-02T00:00:00+08:00",
      "forecast_value": 212.284
    }
  ]
}
```

> **Selaras dengan [KONTRAK_API_v1.md](KONTRAK_API_v1.md):** nama field nilai
> mengikuti kontrak API `forecast_value` (bukan `forecast_kw`), dan `unit`
> mengikuti `target`. `kW`/`kWh` hanya muncul setelah gate §10.1–10.3 di
> kontrak API diputuskan.

**Field wajib respons:**

| Field | Tipe | Keterangan |
|---|---|---|
| `site_id` | `str` | Echo dari request |
| `target` | `enum` | `irradiance` (wajib — kontrak API §3.2; menandai derivasi `unit`/`order`) |
| `ts` | `datetime` tz-aware | Batas akhir data input |
| `generated_at` | `datetime` tz-aware | Saat forecast dibuat — **wajib terpisah dari `ts`** agar akurasi bisa diukur |
| `resolution` | `enum` | `daily` \| `hourly` |
| `horizon` | `int` | = `len(forecast)` |
| `unit` | `str` | Ikut `target`: `W/m2` untuk `irradiance` (bukan `kW` — lihat §4.2/§4.4; `kW` menunggu gate §10 kontrak API) |
| `timezone` | `str` | IANA string |
| `model_version` | `str` | Semver |
| `forecast` | `list[obj]` | Panjang = `horizon`, urut kronologis naik |
| `forecast[].forecast_time` | `datetime` tz-aware | Tidak boleh duplikat |
| `forecast[].forecast_value` | `float` | 3 desimal |

**Field opsional respons:** `order` (mis. `ARIMA(1,0,2)`), `forecast_id`
(hanya pada `latest`/`history`, tidak pada `POST` on-demand), `mae`, `rmse`,
`mape` — ditambahkan tanpa mengubah kontrak lama.

> **Terverifikasi end-to-end 2026-10-06** (`scripts/validate_e2e.py`, 57
> pemeriksaan terhadap server live di `:8000`): seluruh field di tabel ini
> benar-benar muncul pada respons nyata.

---

## 3. ATURAN VALIDASI (Fase 5 — test kontrak)

| ID | Aturan | Kegagalan jika dilanggar |
|---|---|---|
| C-01 | `ts` dan semua `forecast_time` wajib tz-aware | `422` |
| C-02 | Zona identik di seluruh request/respons (`Asia/Makassar`) | `422` |
| C-03 | `len(forecast) == horizon` | `500` |
| C-04 | `forecast_time` unik dan terurut naik | `500` |
| C-05 | `0 ≤ forecast_value ≤ batas atas target` | `500` |
| C-06 | `irradiance` dan `temperature` dalam rentang §1.1 | `422` |
| C-07 | `horizon` ≤ 30 (daily) | `422` |
| C-08 | `power_history` tidak kosong dan tanpa `NaN` | `422` |
| C-09 | `model_version` cocok dengan pola semver | `500` |
| C-10 | Respons identik skema-nya walau model diganti (uji swap model) | Kontrak rusak |

---

## 4. TEMUAN YANG MEMBATASI KONTRAK

### 4.1 Zona waktu salah pada template awal

Template menyatakan `Asia/Jakarta` (UTC+7). PLTS berada di **Kupang, WITA = UTC+8**.

- Zona yang benar: **`Asia/Makassar` (UTC+8)**
- Notebook membagi tiga zona: WIB (UTC+7), **WITA (UTC+8, wilayah Kupang)**, WIT (UTC+9)
- Data sumber `solcast1.csv` memakai **UTC** pada `period_end` — konversi wajib dilakukan sebelum masuk model

**Kontrak memakai `Asia/Makassar`.** Jika tetap memakai `Asia/Jakarta`, seluruh jam forecast meleset 1 jam dan filter siang 07:00–17:00 ikut salah.

#### Aturan zona waktu tunggal (berlaku di seluruh sistem)

Larangan timestamp naive dan penanganan multi-zona SCADA/inverter/meter/cuaca
dikunci di [KONTRAK_API_v1.md](KONTRAK_API_v1.md) **§1.5**. Ringkasnya:

| Aturan | Implementasi |
|---|---|
| Semua timestamp membawa offset eksplisit | `coerce_timezone()` **menolak** index naive; zonanya tidak pernah ditebak |
| Zona masukan boleh berbeda-beda | `tz_convert()` — SCADA `Z`, inverter `+07:00`, cuaca `+08:00` semua disamakan, bukan ditolak |
| Simpan & bandingkan dalam UTC | Konversi sebelum join/urutan/interval |
| Serialisasi respons zona site | `+08:00` untuk `PLTS_KUPANG` (`timezone: "Asia/Makassar"`) |

Jaminan diuji: `python test_timezone_contract.py` (momennya sama dari zona
berbeda → output identik; index naive → ditolak; engine == preprocessing ==
kontrak API).

### 4.2 `power_history` dalam kW belum memiliki sumber data

Data energi yang tersedia **harian dan berbentuk kWh, bukan kW**:

| Sumber | Kolom | Rentang | Resolusi |
|---|---|---|---|
| `A1_transaksi_harian_clean.csv` | `delivered_energi` | 14.536 – 21.278 | Harian (kWh) |
| `A1_transaksi_harian_clean.csv` | `delivered_kwh_jam10` | 14.537 – 9.996.369 | Tampak seperti pembacaan meter kumulatif, **bukan** energi per hari |

Tidak ditemukan deret daya kW pada resolusi apa pun.

**Pilihan untuk target energi (salah satu wajib dipilih sebelum kontrak API di-freeze):**

| Opsi | Definisi `power_history` | Konsekuensi |
|---|---|---|
| **A (disarankan)** | Energi harian kWh per step (tanpa ÷ durasi) → field diganti nama jadi `energy_history_kwh` | Jujur terhadap data; `forecast_kw` diubah menjadi `forecast_kwh` |
| B | kWh ÷ 24 jam → rata-rata daya kW | Angka turunan; identitas "daya" jadi lemah |
| C | Menyediakan data inverter 15-menit/5-menit dari monitoring platform | Paling benar secara domain; menunggu data |

**Rekomendasi untuk kontrak energi:** Opsi A paling sesuai dengan data yang tersedia, tetapi **belum diputuskan**. Draft kontrak API v1.0.0 saat ini hanya mempublikasikan `irradiance` dalam `W/m2`; `energy_kwh`/`power_kw` tetap tidak aktif sampai gate §10.2–10.3 pada [KONTRAK_API_v1.md](KONTRAK_API_v1.md) disepakati.

### 4.3 Satuan irradiance berbeda antar sumber

| Sumber | Kolom | Satuan | Rentang |
|---|---|---|---|
| Solcast 5-menit | `ghi` | W/m² (instantan) | 0 – 1015 |
| CAMS harian | `ghi_cams` | Wh/m² per hari (iradiasi) | 680 – 8.259 |
| Solcast harian | `ghi_solcast_mean` | W/m² (rata-rata harian) | 34.66 – 299.71 |

Ketiganya **tidak boleh dicampur**. Kontrak memakai **W/m² rata-rata**, sehingga `ghi_cams` harus dibagi 24 (atau dikonversi sebelum masuk).

### 4.4 Dua notebook tidak punya keluaran `power` sama sekali

Keduanya memprediksi **irradiance**, lalu mengestimasi energi via `estimate_energy()` dengan parameter tetap (area 1 m², efisiensi 20% / kapasitas 1 kWp, PR 1.0). Parameter itu **belum dikalibrasi** ke PLTS Kupang. Sebelum `forecast_kw` bisa diyakini, faktor kalibrasi (kapasitas nyata × PR) wajib ditentukan.

---

## 5. SUMBER DATA RESMI (untuk pengisian `power_history`)

| Kebutuhan | File | Periode | Resolusi |
|---|---|---|---|
| Energi harian | `Processed/A1_transaksi_harian_clean.csv` | 2024-01-01 – 2026-01-01 | Harian |
| GHI + suhu + tutupan awan | `Processed/B1_daily_master_2016_2026.csv` | 2016-01-01 – 2026-01-31 | Harian |
| GHI 5-menit | `SEI/solcast1.csv` | 2023-01-01 – 2025-12-31 | 5 menit |
| GHI hourly | `CAMS/Kupang_CAMS_Hourly_*.csv` | Terbatas | Per jam |
| Data bergabung | `Processed/B2_master_transaksi_cuaca.csv` | 2024–2025 | Harian |

---

## 6. VARIAN HOURLY (Fase 2 — belum dikunci)

Berlaku jika endpoint `resolution: "hourly"` ditambahkan:

| Aspek | Nilai |
|---|---|
| Resolusi step | 1 jam |
| Jam operasional | 07:00 – 17:00 WITA (**11 step/hari**, mengikuti filter notebook) |
| `horizon` | 1 – 11 step per hari operasional, atau 1 – 168 (7 hari) |
| Model | SARIMA, seasonal period = 11 |
| Status | Belum tervalidasi — MAPE tercatat 9.64% – 28.3% |

---

## 7. RINGKASAN KEPUTUSAN UNTUK FASE 1

```
RESOLUSI   : daily (v1)
ZONA       : Asia/Makassar (UTC+8)     ← bukan Asia/Jakarta
INPUT      : site_id, ts, irradiance, temperature, power_history, horizon
TARGET     : irradiance (W/m2) — SATU-SATUNYANYA yang dipublikasikan di v1.0.0
             energy_kwh / power_kw masih GATE (KONTRAK_API_v1.md §10.1–10.3)
FORMAT     : JSON, daftar {forecast_time, forecast_value}   ← lihat KONTRAK_API_v1.md
HORIZON    : 1..30 step (harian)
VERSIONING : semver wajib pada setiap respons
```

**Empat hal yang harus dipastikan sebelum kontrak API v1.0.0 di-freeze:**

1. Konfirmasi zona `Asia/Makassar` (§4.1)
2. Pilih Opsi A/B/C untuk `power_history` (§4.2) → gate §10.2 kontrak API
3. Tentukan faktor kalibrasi energi dari irradiance (§4.4) → gate §10.3 kontrak API
4. Setujui retensi scheduler & rate limit (kontrak API §10.5–10.6)

---

## 8. STATUS IMPLEMENTASI (Fase 1 — selesai)

Mesin forecasting sudah dibuat dan diuji. Berkas:

| Berkas | Isi |
|---|---|
| `forecast_engine.py` | Mesin murni & deterministik (hanya menerima data bersih) |
| `test_forecast_engine.py` | Uji determinisme/kemurnian/kontrak — `python test_forecast_engine.py` |
| `preprocessing.py` | Resampling, interpolasi, fitur, normalisasi — terpisah dari engine |
| `test_preprocessing.py` | Uji preprocessing & integrasi — `python test_preprocessing.py` |
| `test_timezone_contract.py` | Uji aturan zona tunggal (multi-zona masuk, naive ditolak, kesetaraan modul) — `python test_timezone_contract.py` |
| `evaluation.py` | Strategi evaluasi: rolling backtest tanpa kebocoran + ambang GO/NO-GO |
| `test_evaluation.py` | Uji backtest/ambang/kemurnian — `python test_evaluation.py` |
| `STRATEGI_EVALUASI.md` | Metrik utama, skema backtest, ambang, dan hasil nyata — dokumen kembar di sini |
| `KONTRAK_API_v1.md` | Kontrak HTTP v1 (endpoint, kode error, pagination, gate freeze, §1.5 zona waktu) — dokumen kembar di sini |

### 8.1 Fungsi inti

```python
def generate_forecast(
    historical_data: pd.DataFrame,     # index DatetimeIndex tz-aware, kolom target
    weather_data: pd.DataFrame | None, # opsional, exog; wajib menutup jendela forecast
    horizon: int = 24,                 # 1..30 untuk harian
    *,
    target: str = "irradiance",
    order: tuple[int, int, int] = (1, 0, 2),   # urutan WITA
    exog_cols: Iterable[str] = (),
    model_version: str = "v1.0.0",
) -> pd.DataFrame: ...
```

Kolom output: `forecast_time`, `forecast_value`, `model_version`.

### 8.2 Jaminan yang sudah diverifikasi (34 pemeriksaan engine + 82 pemeriksaan preprocessing + 31 pemeriksaan zona waktu — semua lulus)

| No | Jaminan | Cara dibuktikan |
|---|---|---|
| 1 | Dua panggilan input sama → output identik | `DataFrame.equals`, JSON identik, array bitwise identik |
| 2 | Hasil tidak bergantung urutan pemanggilan | Panggilan lain di antaranya tetap menghasilkan output sama |
| 3 | Input tidak dimodifikasi | Frame asli dicek `equals` terhadap salinan |
| 4 | Tidak ada `print()` | `redirect_stdout`+`redirect_stderr` ditangkap, 0 karakter |
| 5 | Tidak ada I/O file | `builtins.open` di-spies, 0 pemanggilan; daftar file tidak berubah |
| 6 | Tanpa RNG | Pemeriksaan AST pada kode yang dieksekusi: bebas `random`/`seed`/`np.random` |
| 7 | Kontrak input ditolak bila melanggar | tz naive, horizon 0/31, kolom hilang, history < 30, gap waktu, nilai negatif |
| 8 | Kontrak output | urut naik, unik, tz `Asia/Makassar`, langkah 1 hari, ≥ 0, 3 desimal |
| 9 | Determinisme dengan exog cuaca | Identik antar panggilan; exog tanpa jendela penuh ditolak |
| 10 | Semua urutan zona | WIB/WITA/WIT masing-masing deterministik |

### 8.3 Pipeline preprocessing terpisah

Modul [preprocessing.py](preprocessing.py) **tidak** mengubah atau mengimpor logika forecast. Ia menerima DataFrame kasar dan mengembalikan DataFrame reguler/bersih, siap diberikan sebagai `historical_data` ke `generate_forecast`. Fungsi-fungsinya juga dapat dipakai ulang saat membangun data aktual untuk uji akurasi:

```
DataFrame mentah
   └─ coerce_timezone()        → zona waktu konsisten (Asia/Makassar)
      └─ resample_regular()     → interval tetap; bin kosong tetap NaN
         └─ interpolate_gaps()  → isi gap internal; tolak gap tepi secara default
            └─ make_features()  → lag, rolling mean/std, fitur sin/cos tahunan
               └─ fit_normalization() / apply_normalization()
                    └─ PreparedData.frame
                         └─ generate_forecast()
```

- **Tidak membaca/menulis file, tidak mencetak, tanpa RNG**, input tidak dimutasi; semua fungsi deterministik.
- **`resample_regular` tidak mengubah bin kosong menjadi nol.** Ia menghitung agregasi dan hitungan per-bin secara terpisah, karena pandas 3.0.1 `Resampler.mean()` tidak menerima `min_count`.
- **`interpolate_gaps` menangani perilaku pandas 3.0.1 yang terverifikasi:** `interpolate(method="time")` mengisi trailing NaN dengan membawa nilai terakhir. Default preprocessing sekarang mendeteksi NaN di kepala/ekor **sebelum interpolasi** dan menolak. `allow_extrapolate=True` eksplisit bila carry-forward/backfill memang dikehendaki.
- **`make_features` sengaja membuat NaN di baris awal lag/rolling**, karena belum ada masa lalu; fungsi tidak mengarang nilai. Target sendiri tetap harus bebas NaN agar engine menerima input.
- **Normalisasi dibagi `fit_normalization` dan `apply_normalization`.** Statistik wajib di-fit pada data train dan disimpan/dipakai ulang untuk data uji; jangan fit ulang pada actual/evaluation data (data leakage). `inverse_normalization` mengembalikan ke satuan fisik.
- `PreparedData` mengembalikan `{frame, stats, meta}`; metadata merekam resolusi, jumlah baris, gap, dan jumlah langkah.

Uji terpisah:

```bash
python test_preprocessing.py
```

Uji mencakup resampling, gap internal vs tepi, nilai negatif, fitur, normalisasi train/test, determinisme, validasi, dan aliran `prepare_dataset → generate_forecast`. **Semua pemeriksaan preprocessing lulus.** Smoke test tambahan juga lulus pada CSV proyek `B1_daily_master_2016_2026.csv`: 1.096 baris (rentang tanggal yang punya overlap Solcast/suhu), daily cadence, WITA; hasil 7 hari identik pada dua pemanggilan.

### 8.4 Alasan desain yang berubah dari notebook

| Keputusan | Notebook | Modul | Alasan |
|---|---|---|---|
| Pemilihan order ARIMA | Grid search di dalam fungsi, tiap run | Konstanta `MODEL_ORDERS` | Grid search membuat latensi dan hasil bergantung input; order diambil dari hasil tervalidasi notebook (WIB `3,0,0` MAPE 6.49%, WITA `1,0,2` MAPE 6.44%, WIT `2,0,1` MAPE 7.50%) |
| Interval langkah | Asumsi harian | Dihitung dari index, wajib seragam | Modul juga bisa menerima data non-harian tanpa salah asumsi; gap waktu ditolak eksplisit |
| Input naive (tanpa tz) | ditoleransi | Ditolak `ValueError` | Kontrak C-01/C-02; tanpa ini hasil tidak dapat dibandingkan antar run |

**Sudah dikembalikan ke perilaku skrip** (tahap "pindah modul tanpa ubah
logika"): `enforce_stationarity=False`, `np.clip`, `nan_to_num`, dan
pembulatan 3 desimal **tidak lagi ada** di `app/engine.py`. Pengukuran saat
perpindahan menunjukkan `enforce_stationarity` sendiri menggeser hasil
sebesar **0.2944** (dari maksimum selisih 0.2948) — bukan efek yang boleh
diabaikan pada tahap yang mensyaratkan selisih nol.

Aturan kontrak **tetap berlaku**, hanya dipindahkan ke lapisan API:

| Aturan kontrak | Lokasi lama (engine) | Lokasi sekarang |
|---|---|---|
| Pembulatan 3 desimal | `np.round(..., DECIMALS)` | `app/schemas.py` (`ForecastPoint.forecast_value`) |
| Klip `forecast_value >= 0` (C-05) | `np.clip(values, 0, None)` | `app/api/forecast.py` `run_forecast()` |

Kesetaraan modul↔skrip diuji permanen oleh `tests/test_module_parity.py`
(engine, preprocessing, pipeline ujung-ke-ujung, dan tiga zona — semuanya
`max|Δ| = 0.0`).

### 8.5 Batas yang belum dicakup

- **Endpoint sudah diimplementasikan** di `app/` — lihat [KONTRAK_API_v1.md](KONTRAK_API_v1.md); gerbang §10 masih terbuka sebelum freeze v1.0.0 FINAL.
- **Belum ada pembacaan data.** Modul menerima `DataFrame` — membaca CSV jadi tugas lapisan API.
- **Belum ada `power_history`/kW.** Kontrak §4.2 masih terbuka; modul memprediksi `target` apa pun yang diberikan.
- **Belum ada grid search.** Bila urutan perlu dicari ulang, lakukan di script evaluasi terpisah, bukan di jalur request API.
- **Belum ada validasi MAE/RMSE/MAPE final.** Pipeline preprocessing→engine sudah di-smoke-test pada `B1_daily_master_2016_2026.csv` (1.096 baris, deterministik), tetapi angka metrik pada `KONTRAK_API_v1.md` §5.5 masih placeholder sampai pipeline walk-forward Fase 5 berjalan.

