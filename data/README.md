# data/

Direktori untuk data yang dipakai service (forecast history tersimpan, cache
evaluasi). **Tidak** berisi dataset mentah proyek — lihat `../Data-*`.

Isi yang diharapkan fase berikutnya:

    data/
    ├── forecasts/        # bila storage di-filifykan (SQLite/Parquet)
    └── evaluation/       # cache laporan backtest yang sudah pernah jalan

Aturan: file mentah dari luar repo cukup dirujuk, jangan digandakan.
