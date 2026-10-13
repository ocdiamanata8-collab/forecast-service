# model/

Direktori artefak model — tiga berkas, tiga pembaca masing-masing.

| Berkas | Dibaca oleh | Fungsi |
|---|---|---|
| `VERSION` | `app/version.py` | **Sumber kebenaran versi** (baris pertama) + metadata |
| `artifacts.json` | `app/artifacts.py` | Fitur, **urutan kolom**, skala, order, lingkup training |
| `model.pkl` | `scripts/export_model.py` (penulis) | Snapshot hasil fit — inspeksi/reproduksi |

Semuanya ditulis oleh `python scripts/export_model.py`.

## artifacts.json — sumber kebenaran urutan fitur

Ini bagian yang menjawab "hasil API aneh":

```json
"columns":     ["irradiance", "temperature", "tcc"],
"exog_columns":["temperature", "tcc"],
"dtypes":      {"irradiance": "float64", ...},
"scaling":     {"method": null, "params": {}}
```

API **tidak pernah menebak** urutan kolom. `app/api/forecast.py` membentuk
frame exog mengikuti `exog_columns`, terlepas dari urutan payload client.
Tanpa ini, statsmodels menempatkan exog berdasarkan **posisi** — kolom bisa
tertukar diam-diam, forecast tetap terlihat masuk akal, tapi dihitung dengan
fitur yang salah.

Dijamin oleh `tests/test_artifacts.py`:
- payload exog terbalik → forecast **identik**
- kolom di luar daftar → `422` (bukan diabaikan)
- kolom kurang → `422 E_EXOG_MISSING_COLUMN`
- target tak cocok → `422 E_TARGET_UNAVAILABLE`

`scaling.method = null` = model bekerja pada satuan asli (tanpa normalisasi).
Bila kelak model dinormalisasi, isi `mean`/`std` per kolom di sini —
`app/artifacts.apply_scaling()` akan menerapkannya sebelum fit, sehingga
skala request selalu sama dengan skala training.

## model.pkl — mengapa ada, dan apa yang TIDAK dilakukannya

Berbeda dari catatan lama: berkas ini **ada** (±2,6 MB), dihasilkan
`scripts/export_model.py`. Isinya objek hasil fit ARIMA penuh, dibuat dari
data kanonik 1.096 baris (2023-01-01 – 2025-12-31).

**Runtime tidak memuat `model.pkl`.** `app/engine.py` tetap melakukan refit
per request — itu syarat determinisme yang dibuktikan
`tests/test_module_parity.py` (selisih nol dengan skrip asli). Snapshot ini
untuk inspeksi, reproduksi riset, dan bukti bahwa artefak pernah dibuat.

Bila kelak ada model yang **fitting sekali, dipakai banyak kali** (XGBoost,
LSTM, kalibrasi mahal), polanya: simpan `model.pkl`, isi `artifacts.json`,
lalu muat di lifespan — endpoint tidak berubah.

Jangan commit model biner besar ke git; gunakan LFS atau objek storage di
fase produksi.

## Mengubah data/kolom training

    python scripts/export_model.py       # tulis ulang model.pkl + artifacts.json

Setelah itu `GET /api/v1/model/info` otomatis melaporkan fitur dan urutan
baru; API menolak request exog yang tidak cocok dengan artefak segar.
