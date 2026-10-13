# PERAN DASHBOARD ENGINEER

**Berkas ini mengikat desain `templates/index.html`. Perubahan peran harus
diubah di sini lebih dulu, lalu kodenya.**

---

## Satu kalimat

Halaman untuk **engineer menguji dan menyetel forecast** — bukan untuk
pengguna akhir membaca forecast.

## Empat fungsi resmi (hanya empat)

| # | Fungsi | Implementasi |
|---|---|---|
| 1 | **Memilih site** | Dropdown `site_id` + opsi ketik manual; semua pemanggilan memakai site terpilih |
| 2 | **Mengubah horizon** | Input numerik 1..30; dipakai `POST /forecast` |
| 3 | **Menjalankan forecast manual** | Tombol → `POST /api/v1/forecast` dengan history yang disediakan engineer |
| 4 | **Membandingkan hasil dengan data aktual** | Panel Evaluasi → `GET /api/v1/model/performance`: MAE / RMSE / MAPE / skill per horizon, plus ambang dan status GO/NO-GO |

Fungsi lain di halaman (audit kontrak, kesehatan layanan, log) adalah
**alat bantu engineer**, bukan tujuan — tetap boleh ada selama tidak
bersaing dengan empat fungsi di atas.

## Batas keras: jangan campur dengan monitoring pengguna akhir

Forecast untuk **pengguna akhir ditampilkan oleh platform monitoring utama**
(ELK Grafana, dsb). Dashboard ini:

- **TIDAK** dirancang untuk menampilkan forecast ke pengguna akhir.
- **TIDAK** membagikan tampilan/URL kepada pengguna akhir.
- **TIDAK** menjadi "dashboard produksi". Ia sengaja berisi tombol manual,
  input teknis, dan angka mentah yang tidak bermakna bagi non-engineer.
- Panel "Forecast terbaru" ada hanya agar engineer **memverifikasi**
  endpoint `latest` yang sama dengan yang dipakai monitoring — bukan untuk
  menayangkan forecast ke publik.

Kalau suatu hari ada permintaan "tampilkan forecast ini ke atasan/direksi",
jawabannya **bukan** menambah panel di sini, melainkan menunjuk platform
monitoring utama. Itulah batas yang mencegah dua tampilan tumbuh berbeda
untuk pengguna yang sama.

---

## Keterbatasan yang diakui (jangan disembunyikan)

**Perbandingan per-titik kini tersedia** melalui `GET /api/v1/forecast/actual`
(tambahan non-breaking, kontrak §5.7); grafik §2b memakainya.

Keterbatasan yang **masih berlaku**:

- **Rentang data aktual terbatas: 2023-01-01 – 2025-12-31.** Forecast di luar rentang ini tidak punya pasangan aktual; grafik menampilkan jumlah tanggal tanpa aktual dan memutus garis — tidak mengarang angka.
- **Pencocokan strict timestamp**: hanya waktu yang sama persis setelah normalisasi timezone; tidak ada interpolasi atau nearest-date.
- **Band grafik = selisih absolut |aktual−forecast|**, bukan confidence/prediction interval. Engine v1 hanya menghasilkan titik forecast.
- **Browser tidak membaca CSV.** Data aktual disajikan server-side dari `data/actuals.csv`, yang dibuat oleh `scripts/prepare_actuals.py`.
- Fungsi #4 agregat (MAE/RMSE/MAPE/skill) tetap dibaca dari `GET /api/v1/model/performance`; grafik memberi konteks per-titik tambahan, bukan mengganti evaluasi agregat.
- Batas peran tetap: grafik ini alat engineer, forecast pengguna akhir hanya ditampilkan lewat platform monitoring utama.

---

## Pemeriksaan kepatuhan

`tests/test_dashboard.py` memeriksa:

- halaman memuat `POST /api/v1/forecast` (fungsi #3)
- ada kontrol pemilih `site` (fungsi #1) dan `horizon` (fungsi #2)
- halaman memuat `/api/v1/model/performance` (fungsi #4)
- ada teks pemisahan peran (monitoring pengguna akhir ≠ halaman ini)
- halaman tetap konsumen API murni (tanpa data forecast yang dibakar)
- grafik SVG ada, memanggil `/api/v1/forecast/actual`, berlabel
  "selisih absolut" (bukan confidence), tanpa CDN, dengan legend + tabel
  accessible
- klaim basi "perbandingan per-titik belum tersedia" tidak boleh muncul lagi
