# STRATEGI EVALUASI & AMBANG BATAS
### Forecasting Service PLTS Kupang (v1)

| | |
|---|---|
| Dokumen | `STRATEGI_EVALUASI.md` |
| Versi | 1.0 |
| Tanggal | 2026-10-02 |
| Fungsi | Menentukan **sebelumnya**: metrik utama, cara backtest, dan ambang lulus/tidak. Diuji di Fase 5. |
| Modul | [evaluation.py](evaluation.py) · Uji: [test_evaluation.py](test_evaluation.py) (49 pemeriksaan, lulus) |
| Terkait | [KONTRAK_API_v1.md](KONTRAK_API_v1.md) §5.5 (field respons performance) · [SPESIFIKASI_KONTRAK_MODEL.md](SPESIFIKASI_KONTRAK_MODEL.md) |

> **Kenapa dokumen ini ada duluan:** tanpa ambang yang ditulis sebelum angka
> dihitung, Fase 5 berakhir jadi diskusi tanpa keputusan. Ambang di bawah
> **disusun sebelum backtest dijalankan** (lihat §3.1); angka §5 baru kemudian
> menghadapinya. Hasil yang tidak lulus akan tetap tidak lulus — tidak
> ada ambang yang dinaikkan setelah tahu hasilnya.

---

## 1. METRIK UTAMA

| # | Metrik | Peran | Dipegang sebagai |
|---|---|---|---|
| **1** | **MAE** | Ukuran kesalahan absolut, satuan asli target (W/m²) | **Utama — penentu gerbang** |
| **2** | **MAPE** | Ukuran kesalahan relatif terhadap aktual (%) | **Utama — penentu gerbang** |
| 3 | RMSE | Penalti eksponensial pada kesalahan besar | Dilaporkan, **bukan** gerbang |
| 4 | Skill vs persistence | `1 − MAE_model/MAE_baseline` | **Gerbang minimum** — model wajib minimal setara baseline |
| 5 | Skill vs seasonal naive | Sama, terhadap baseline musiman (h ≤ 7) | Informasional |

**Alasan MAE dan MAPE dipilih sebagai pasangan** — keduanya menangkap hal
berbeda dan saling melengkapi:

- **MAE** dalam satuan target: `38.9 W/m²` berarti berapa banyak energi surya
  yang salah perkiraan. Kenaikan 10 W/m² ≈ 10 W menghasilkan 10 Wh/m² per jam —
  langsung bisa dikonversi ke rupiah oleh tim perencanaan.
- **MAPE** mencegah kemenangan semu. Pada irradiance harian yang naik-turun
  (34–300 W/m²), MAE yang "oke" bisa jadi mengerikan pada hari mendung, di
  mana nilai dasarnya kecil dan kesalahan relatifnya meledak. Inilah yang
  sesungguhnya terjadi pada data nyata — lihat §5.

**Kenapa RMSE bukan gerbang:** RMSE selalu ≥ MAE dan menghukum outlier dua
kali lipat. Bila dipakai sebagai syarat tunggal, satu badai akan membatalkan
seluruh evaluasi. RMSE tetap dilaporkan untuk mengamati **ekor kesalahan**.

**Kenapa skill termasuk gerbang, bukan sekadar laporan:**
`baseline persistence` = "besok sama seperti hari ini". Model yang tidak bisa
mengalahkannya **tidak ada gunanya** — begitu saja lebih murah dijalankan.
Sebaliknya, model bisa lulus MAE/MAPE hanya karena targetnya memang stabil.
Karena itu dua arah harus lolos: **akurat secara absolut DAN lebih baik dari
baseline**.

---

## 2. CARA ROLLING BACKTEST

### 2.1 Skema — expanding window, holdout terakhir

```
Baris:   0 .......................................... N-holdout ............ N-1
         |----------- TRAIN (expanding) -----------|  |------ HOLDOUT ------|
                                     ^                         ^        ^
                               origin pertama            origin terakhir
                                                       + horizon terpanjang
```

Untuk setiap **origin** `i`:

| Komponen | Isi |
|---|---|
| Data latih | baris `0 .. i` (prefix murni, **hanya masa lalu**) |
| Jendela forecast | baris `i+1 .. i+H` — **wajib seluruhnya di holdout** |
| Data aktual | baris holdout yang jatuh di jendela tersebut |
| `generated_at` | waktu origin tersebut (dipakai untuk mereproduksi urutan) |

Aturan yang **dipastikan oleh kode, bukan oleh kepatuhan pengguna:**

```
origin_min = (N - holdout) - 1          ← forecast pertama JATUH di holdout
origin_max = N - 1 - max(horizons)      ← jendela terpanjang MASIH MUAT
origin     = origin_min .. origin_max   langkah `stride`
```

### 2.2 Pengamanan kebocoran (diuji, bukan dijanjikan)

Empat pemeriksaan di [test_evaluation.py](test_evaluation.py) §2 membuktikan
jalur data:

1. **Spy pada `generate_forecast`** — panjang train per panggilan dicatat.
   Diverifikasi: menaik (bukan shuffle), `train[0] == origin_min + 1`.
2. **Uji invarian isi** — holdout **diubah seluruhnya** ke nilai `1.0`,
   lalu backtest dijalankan ulang:
   - jumlah origin & panjang train **identik** (tidak ada kebocoran pengaruh)
   - metrik **berbeda** (aktual memang dipakai untuk mengukur, wajar)
3. **Origin dihitung dari `N - holdout`**, bukan diset manual — tidak bisa
   salah konfigurasi diam-diam.
4. **`holdout < horizon terbesar` → ditolak** — jendela yang tidak muat
   dipastikan gagal, bukan memaksa jendela terpotong.

### 2.3 Konfigurasi bawaan

| Parameter | Nilai | Alasan |
|---|---|---|
| `holdout` | **365 hari terakhir (2025-01-01 – 2025-12-31)** | Satu tahun penuh mencakup 4 musim/ENSO, musim hujan & kemarau |
| `stride` | **1** (setiap hari jadi origin) | Sampel maksimal; deterministik, jadi tidak ada keuntungan acak dari pemilihan origin |
| `horizons` | **1, 7, 14, 30** hari | 1 = operasional harian, 7 = perencanaan mingguan, 30 = horizon kontrak |
| `primary_horizon` | **7 hari** | Sesuai pola `rolling forecast` per jam/hari untuk EMS |
| `order` | `ARIMA(1,0,2)` | Urutan WITA terpilih dari notebook (AIC 3586.65) |
| Baseline | persistence + seasonal naive (periode 7) | Dihitung di dalam backtest yang sama, pada origin yang sama |
| Target | `irradiance` (`W/m²`) | Satu-satunya target yang dipublikasikan di kontrak v1 |

### 2.4 Determinisme & kemurnian

`rolling_backtest` mengikuti aturan semua modul: **tanpa file, tanpa print,
tanpa RNG, dan hasil identik pada panggilan identik.** Diverifikasi
`test_evaluation.py` §3 dan §9.

`stride` sengaja **dipilih eksplisit** (bukan acak), dan origin dihitung
secara aritmetika dari `N` — sehingga pemilihan sampel pun tidak bisa
berubah antar run.

---

## 3. AMBANG BATAS

### 3.1 Nilai (disusun **sebelum** backtest §5 dijalankan)

```python
DEFAULT_THRESHOLDS = Thresholds(
    max_mae        = 40.0,   # W/m²   — horizon utama (h=7)
    max_mape       = 10.0,   # %      — horizon utama (h=7)
    max_mape_long  = 15.0,   # %      — horizon terpanjang (h=30), lebih longgar
    min_skill      =  0.0,   # skill  — minimal sebanding persistence
    primary_horizon = 7,     # hari
)
```

**Dasar penetapan (independen dari hasil §5):** notebook harian melaporkan
walk-forward satu-langkah WIB/WITA/WIT dengan **MAPE 6.44–7.50%** dan
**MAE 28.41–32.35 W/m²**. Ambang diberi **ruang wajar ±25–40%** di atas
pencapaian tercatat:

- `max_mape = 10%` → sekitar **1.4×** dari MAPE terbaik (6.44%)
- `max_mae = 40` → sekitar **1.25×** dari MAE terburuk (32.35)
- `max_mape_long = 15%` → horizon 30 hari memang lebih sulit, masih diberi
  ruang, tetapi tetap dibatasi (tanpa batas panjang = horizon panjang bebas)

Ruang itu berarti model boleh sedikit memburuk dibanding catatan notebook
tanpa langsung gugur, tetapi tidak boleh memburuk lebih dari ~40%.

### 3.2 Ambang skill

`min_skill = 0.0` — artinya **wajib minimal sebanding** dengan persistence
(`skill ≥ 0`). Ini ambang terendah yang masih masuk akal; menaikkannya
(mis. `min_skill = 0.10` = 10% lebih baik) adalah pilihan bisnis yang boleh
dilakukan setelah baseline di ukur nyata. Menurunkannya di bawah 0 tidak
mungkin di program — `Thresholds.validate()` menolak `min_skill < 0`,
karena model yang lebih buruk dari baseline tidak layak dipakai apa pun
parameter lainnya.

### 3.3 Aturan penilaian

`judge_backtest()` menghasilkan daftar `failures`. **Semua** butir harus
kosong untuk dianggap lulus; satu gagal = **NO-GO**.

| # | Pemeriksaan | Gagal bila |
|---|---|---|
| G-1 | MAE horizon utama | `> max_mae` |
| G-2 | MAPE horizon utama | `> max_mape` |
| G-3 | MAPE horizon terpanjang | `> max_mape_long` |
| G-4 | Skill horizon utama | `< min_skill` atau `NaN` (MAE baseline 0) |

**Status:**

| Status | Syarat | Konsekuensi |
|---|---|---|
| **GO** | `failures == []` | Model **boleh** dijadikan sumber monitoring; angka diisi ke `GET /api/v1/model/performance` |
| **NO-GO** | ada `failures` | Model **tidak** layak untuk monitoring. Boleh tetap dipakai untuk riset/eksperimen, dengan penandaan eksplisit |

---

## 4. CATATAN INTEGRITAS METRIK

### 4.1 Oracle exog (WAJIB dibaca sebelum memakai angka exog)

Saat `exog_cols` diisi, backtest memakai nilai cuaca **aktual** pada jendela
forecast. Di produksi cuaca datang sebagai **prakiraan**, yang punya error
sendiri. Karena itu skill dengan exog **selalu terlalu tinggi** — skenario
**oracle**.

- Laporan menandainya lewat `report.exog_oracle == True`.
- **Dilarang** memakai metrik oracle sebagai satu-satunya dasar menaikkan
  ambang §3.1.
- Gerbang resmi memakai backtest **tanpa exog** (murni tanpa cuaca) supaya
  angkanya dapat direproduksi dan tidak berhutang pada kualitas prakiraan.

### 4.2 Jangan bandingkan dengan angka notebook tanpa menyetarakan

Angka §5 berasal dari data yang **tersedia di repo ini** (Solcast GHI harian).
Notebook memakai **ERA5 `SSRD_2425.nc`** yang tidak ada di repo, dengan
filter 07:00–17:00 dan rata-rata spasial tiga wilayah. Keduanya **bukan
deret yang sama** — angkanya tidak seharusnya identik. Persis perbedaan inilah
mengapa ambang diberi ruang, dan mengapa §5 melaporkan keduanya terpisah.

### 4.3 Holdout harus sama sepanjang pengembangan

Holdout `2025-01-01 – 2025-12-31` **tidak boleh** dipakai untuk melatih
atau mengatur hyperparameter. Menyentuh holdout saat menyetel model =
holdout harus diganti dengan tahun lain, dan metrik lama dibuang. Ini aturan
yang membuat Fase 5 tetap berarti.

---

## 5. HASIL PADA DATA PROYEK (dijalankan 2026-10-02)

Sumber: `Data/Processed/B1_daily_master_2016_2026.csv`, kolom
`ghi_solcast_mean` → `irradiance`, suhu → `temperature`,
**n = 1.096 baris (2023-01-01 – 2025-12-31)**, zona `Asia/Makassar`.

**Konfigurasi:** `holdout=365` (2025-01-01 – 2025-12-31), `stride=1`,
`order=ARIMA(1,0,2)`, **n = 336 origin**, train pertama 731 baris.

### 5.1 Metrik (tanpa exog — gerbang resmi)

| horizon | MAE | MAPE | RMSE | baseline MAE | skill | n |
|---:|---:|---:|---:|---:|---:|---:|
| 1 hari | 40.513 | **25.69%** | 50.400 | 57.131 | +0.291 | 336 |
| **7 hari** ★ | **40.255** | **25.56%** | 50.022 | 53.222 | +0.244 | 336 |
| 14 hari | 40.561 | 25.69% | 50.209 | 56.626 | +0.284 | 336 |
| 30 hari | 39.750 | 24.39% | 48.954 | 51.931 | +0.235 | 336 |

★ horizon utama.

### 5.2 Hasil penilaian — **NO-GO**

| Gerbang | Ukuran | Ambang | Hasil | Status |
|---|---|---|---|---|
| G-1 MAE h7 | 40.255 | ≤ 40.000 | 40.255 | ❌ **GAGAL** (tipis) |
| G-2 MAPE h7 | 25.56% | ≤ 10.00% | 25.56% | ❌ **GAGAL** (jauh) |
| G-3 MAPE h30 | 24.39% | ≤ 15.00% | 24.39% | ❌ **GAGAL** (jauh) |
| G-4 skill h7 | +0.244 | ≥ 0.000 | +0.244 | ✅ lulus |

**Keputusan: NO-GO untuk monitoring.** Sisi teknis: model **lebih baik dari
persistence** (skill +24%, seasonal +22%), tetapi **tidak cukup akurat secara
absolut** (MAPE ±25% vs ambang 10%).

### 5.3 Dua temuan yang perlu diputuskan sebelum Fase 5

**T-1 — MAPE jauh di atas catatan notebook (25% vs 6.4%) — SUDAH DISELIDIKI.**
Untuk memisahkan "salah implementasi" dari "beda data", saya menjalankan
**metodologi persis notebook** (walk-forward satu-langkah: fit pada history,
forecast 1 langkah, **append aktual**, ulangi) pada data repo — train
2023–2024, test 2025, `ARIMA(1,0,2)`, n=365:

| Sumber | Metode | MAE | MAPE |
|---|---|---:|---:|
| Notebook (ERA5 `SSRD_2425.nc`, tidak ada di repo) | walk-forward 1-langkah, 2024→2025 | 28.41 | **6.44%** |
| Repo data + **metodologi notebook** | walk-forward 1-langkah, 2023-24→2025 | 40.06 | **25.02%** |
| Repo data + backtest modul (h=1) | expanding, holdout 2025 | 40.53 | **25.70%** |

**Kesimpulan: metodologi identik, hasil identik (≈25%) — selisih dengan
notebook bukan karena kode, melainkan karena DATASET.** Deret repo (GHI
Solcast harian Kupang) berbeda dari deret notebook (ER5 SSRD, filter
07:00–17:00, rata-rata spasial tiga wilayah). Reanalisis SSRD umumnya
lebih halus daripada GHI turunan satelit di satu titik — inilah yang membuat
MAPE notebook jauh lebih kecil.

Konsekuensi tindak lanjutnya **bukan** mengubah ambang, tetapi memilih
deret mana yang mewakili target bisnis PLTS Kupang (§5.4 langkah 1).

*Catatan kecil:* angka modul h=1 memakai 336 origin (satu set origin yang sama
untuk semua horizon, dibatasi `origin ≤ N-1-max(h)`), sedangkan walk-forward
notebook memakai 365 hari penuh. Selisih 29 hari terakhir itulah sisa
perbedaan 40.53 vs 40.06 — bukan perbedaan metode.

**T-2 — MAE di persis 40.28 vs ambang 40.00.**
Selisih 0.7% ini bukan kebetulan yang layak ditindaklanjuti; ia menunjukkan
`max_mae=40` dipatok terlalu dekat dengan angka yang akan datang. **Jangan
dinaikkan demi mencapai GO.** Kalau keputusannya ingin MAE memenuhi syarat,
yang benar adalah **memperbaiki model** (exog cuaca dengan prakiraan nyata,
fitur musiman, atau urutan berbeda), lalu mengulang eval — bukan mengendurkan
gerbang.

### 5.4 Dua langkah yang perlu dilakukan sebelum Fase 5

1. **Setarakan dataset.** Ambil ERA5 `SSRD_2425.nc` (atau setara), jalankan
   preprocessing yang sama, ulangi §5.1. Dari situ ketahuan apakah selisih
   berasal dari **data** atau dari **implementasi**.
2. **Putuskan gerbang.** Apakah `max_mape=10%` tetap untuk kontrak v1
   (artinya service belum boleh menyajikan forecast ke monitoring), atau
   ada ambang khusus tahap PoC. **Ini keputusan bisnis, bukan teknis**, dan
   harus dijawab sebelum freeze kontrak (gate §10).

---

## 6. RINGKASAN UNTUK FASE 5

```yaml
METRIK UTAMA   : MAE (W/m²) dan MAPE (%)
BACKTEST       : expanding window, holdout 2025 penuh, stride=1, horizons 1/7/14/30
HORIZON UTA MA : 7 hari
AMBANG         : MAE ≤ 40  ·  MAPE ≤ 10%  ·  MAPE(30h) ≤ 15%  ·  skill ≥ 0
KEPUTUSAN      : seluruh gerbang lulus -> GO; satu gagal -> NO-GO
STATUS SEKARANG: NO-GO (MAPE 25.56% > 10%; MAE 40.255 > 40.000; skill +0.244 lulus)
YANG BOLEH     : membaca angka §5 sebagai diagnostik, memperbaiki model, menyetarakan data
YANG DILARANG  : menaikkan ambang agar lulus; memakai holdout untuk melatih
```

Jalankan sendiri:

```bash
python test_evaluation.py
```
