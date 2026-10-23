# CHECKLIST — MENAMPILKAN PLATFORM MONITORING UNTUK PENGGUNA AKHIR

**SEI · ENERGY INTELLIGENCE PLATFORM**
REV 1.0 · 16 OKT 2026

Melanjutkan **Checklist Master — Forecasting Service**. Master itu menghasilkan
API; checklist ini membangun **tampilan pengguna akhir** di platform monitoring,
dari nol sampai tayang. Sesuai aturan 3·01 master: engineer console tidak
dipakai pengguna akhir — platform monitoring adalah konsumen API.

**Prasyarat dari repo (sudah jadi, jangan dikerjakan ulang):** 3 endpoint
monitoring · scheduler isi otomatis · adapter per-konsumen · key `label:key` ·
jendela horizon `inHorizon`/`validUntil` · guard kontrak di CI.

**Yang belum (pemicu checklist ini):** tampilan pengguna akhir itu sendiri.

---

## FASE A — Akses & Konfigurasi
*Server · Rp0 · ± ½ hari*

| # | Pri | Langkah |
|---|---|---|
| A·01 | P0 | **Simpan API key platform di secret manager.** Satu key per konsumen. Klien hanya mengirim nilai key — labelnya ada di server (`API_KEYS=monitoring:<key>`). Jangan pernah di source code, URL, HTML, log, chat. |
| A·02 | P0 | **Pilih jalur pemanggilan.** Server repo **tidak mengirim CORS** (terverifikasi) → fetch browser lintas-origin diblokir browser. **Rekomendasi: server-side** — backend platform fetch, lalu render ke pengguna. Alternatif: `CORSMiddleware` dengan `ALLOWED_ORIGINS` domain spesifik (bukan `*`, karena ada API key). |
| A·03 | P0 | **Simpan base URL resmi** `https://ocdi.blitz.cloud` sebagai konfigurasi terpusat, bukan hardcode di banyak file. |
| A·04 | P1 | **Ambil adapter terjemahan.** `GET /static/monitoring-adapter.js` dari base URL, atau salin `static/monitoring-adapter.js` dari repo. Inilah satu-satunya berkas yang perlu disentuh bila kontrak berubah. |

## FASE B — Ingesta & Normalisasi
*Backend · Rp0 · ± 1 hari*

| # | Pri | Langkah |
|---|---|---|
| B·01 | P0 | **Fetch tiga endpoint** dengan header `X-API-Key`: `forecast/latest`, `forecast/history?limit=50`, `model/performance`. Elemen `health` boleh tanpa key (probe uptime). |
| B·02 | P0 | **Terjemahkan lewat adapter** — `toForecast`, `toHistory`, `toPerformance`, `toActualSeries`, `toError`. Jangan membaca field kontrak (`forecast_value`, `generated_at`, `horizons[]`) langsung di kode UI: itulah janji "satu berkas". |
| B·03 | P0 | **Simpan / cache hasil.** Untuk B2B: minimal cache per site + `generated_at` (jadwal refresh 5–15 menit). Untuk UI responsif: cache + revalidate background. |
| B·04 | P1 | **Tangani `inHorizon` & `validUntil`** dari adapter — jangan dihitung ulang; aturan jendela horizon sudah terpusat di adapter. |
| B·05 | P1 | **Pertahankan `model_version` & `generated_at`** hingga ke layer tampilan (syarat aturan 6 master: regresi akurasi harus telusur). |

## FASE C — Tampilan Inti Pengguna Akhir
*Frontend · Rp0 · ± 2–3 hari*

| # | Pri | Langkah |
|---|---|---|
| C·01 | P0 | **Grafik produksi: garis forecast sebagai pembanding.** Garis aktual + garis forecast + (opsional) band error absolut. Legenda jelas membedakan keduanya. |
| C·02 | P0 | **Label meta garis forecast:** `model_version` + `generated_at` (+ horizon, `validUntil`) di dekat grafik. Contoh: *Forecast · model v1.0 · dibuat 15 Okt 15:00 +08:00 · horizon 7 hari · valid s.d. 22 Okt*. |
| C·03 | P0 | **Sembunyikan garis forecast di luar jendela horizon** — filter `inHorizon` per titik (jangan digambar, jangan dihitung error-nya). Segmentasi: garis PECAH di batas, jangan dijembatani. Sumber: adapter, jangan re-implementasi. |
| C·04 | P0 | **Titik tanpa aktual = "menunggu pengamatan"**, bukan nol dan bukan garis kosong. Jangan interpolasi / cocokkan tanggal terdekat (sama dengan prinsip grafik repo: tidak boleh mengarang perbandingan). |
| C·05 | P0 | **State service tidak tersedia — eksplisit, bukan angka kosong.** Lihat Fase D. |
| C·06 | P1 | **Kartu ringkasan pengguna:** nilai forecast terbaru (mis. irradiance hari ini/tomorrow), status GO/NO-GO, MAE/MAPE terbaru, waktu pembuatan. Semua lewat adapter, tidak ada mentah. |
| C·07 | P1 | **Riwayat forecast** (halaman/tabs terpisah) dari `toHistory` — tiap entri menampilkan `generated_at` + horizon, memakai filter `from`/`to` server (kini sudah terpasang di kontrak §5.3). |
| C·08 | P1 | **Responsif & aksesibel:** mobile-friendly, tooltip/legenda terbaca, angka pakai `textContent` (bukan `innerHTML`) — nilai API tidak tepercaya. |
| C·09 | P2 | **Perbandingan akurasi** ringkas (opsional di UI pengguna) dari `toPerformance`: MAE/MAPE per horizon, status. Untuk b2b; pengguna akhir cukup badge "model v1.0 · GO". |

## FASE D — Keadaan Gagal & Kadaluarsa (menjawab 5·07 master)
*Frontend + kebijakan · Rp0 · ± 1 hari*

Setiap state punya **pesan yang ditampilkan**, bukan angka kosong/basi:

| # | Pri | Langkah |
|---|---|---|
| D·01 | P0 | **Aturan kadaluarsa tertulis.** Usul: forecast masih berlaku **selama `now ≤ generated_at + horizon hari`** (= `validUntil` adapter). Lewat itu → garis forecast disembunyikan, kartu menampilkan "forecast kedaluwarsa". |
| D·02 | P0 | **State `service tidak tersedia`** (HTTP non-2xx / fetch gagal / cold start): pill merah + pesan jelas, **semua angka & garis lama dibersihkan** — jangan pertahankan nilai hijau basi (bukti: engineer console dulu menyimpan `health ok` + KPI stale saat server mati; sudah diperbaiki & diuji). |
| D·03 | P0 | **State `404 E_NO_FORECAST`**: "forecast belum tersedia — scheduler belum jalan"; berbeda dari outage (ini kondisi kontrak yang sah). |
| D·04 | P0 | **State `403/401`**: "akses ditolak — hubungi admin" — jangan tampilkan detail server. |
| D·05 | P1 | **State `429`**: "permintaan terlalu sering — coba lagi nanti"; pakai `Retry-After`. |
| D·06 | P1 | **State `menunggu pengamatan`** untuk titik forecast tanpa pasangan aktual (C·04). |
| D·07 | P1 | **State cold-start** (Blitz free tier): 503 "The app is awake now" — tampilkan "layanan sedang menyala ulang" + auto-retry. |
| D·08 | P1 | **Auto-recovery:** retry periodik setelah gagal (mis. 30 detik) — UI pulih sendiri setelah service hidup; nilai baru hanya boleh muncul setelah respons sukses. |
| D·09 | P2 | **Timestamp "terakhir diperbarui"** di UI — agar pengguna tahu data umurnya berapa lama, walau cache. |

## FASE E — Kebijakan Data & Kuota
*Keputusan · Rp0 · ± ½ hari*

| # | Pri | Langkah |
|---|---|---|
| E·01 | P0 | **Tetapkan TTL cache platform** (mis. 5–15 menit) < cadangan segar scheduler (per jam) — pengguna tidak pernah menunggu hit langsung ke API pada paint pertama. |
| E·02 | P0 | **Hormati rate limit 60 req/menit per key.** Satu platform = satu key = satu kuota; jangan polling per-pengguna. Batch/aggregate di backend. |
| E·03 | P1 | **Cold start free tier:** keputusan — (a) keep-alive ping, (b) bayar tier always-on, atau (c) terima 30–60 detik dengan pesan D·07. Untuk pengguna akhir 24/7, (b) direkomendasikan. |
| E·04 | P1 | **Data aktual berhenti 2025-12-31** di repo — komunikasikan di UI ("pengamatan hingga 31 Des 2025"); forecast tanggal setelah itu tampil sebagai garis proyeksi tanpa pembanding (C·04). |
| E·05 | P2 | **Retensi history platform** — berapa banyak `toHistory` yang disimpan (bergantung Fase 6 master: database). |

## FASE F — Pengujian
*QA · Rp0 · ± 1 hari*

| # | Pri | Langkah |
|---|---|---|
| F·01 | P0 | **Uji siklus E2E dengan data sungguhan** (pola yang sudah terbukti di repo): data aktual masuk → forecast dibuat → platform menampilkannya → data aktual berikutnya masuk dan dibandingkan → garis aktual + forecast + band tampil. |
| F·02 | P0 | **Uji setiap state kegagalan** (Fase D): matikan service → semua state tampil jelas & angka basi terhapus; balas 404/403/429/503 sintetis → pesan sesuai; pulihkan service → UI auto-recovery. |
| F·03 | P0 | **Uji jendela horizon**: titik di luar `ts + horizon` tidak muncul di garis, band, tabel, maupun tooltip. |
| F·04 | P1 | **Uji multi-site & multi-key**: dua site di `SITE_IDS`, dua key konsumen — pemisahan data & kuota benar. |
| F·05 | P1 | **Uji lintas zona**: semua timestamp tampil `+08:00` (WITA); jangan konversi ke zona browser. |
| F·06 | P1 | **Uji responsif** (mobile 375px, tablet 768px, desktop) + dark mode bila platform memakainya. |
| F·07 | P2 | **Uji beban ringan**: 100 user serentak — cache platform menahan, API tidak kena rate limit. |

## FASE G — Roll-out & Operasional
*Live · biaya hosting · ± 1 hari*

| # | Pri | Langkah |
|---|---|---|
| G·01 | P0 | **UAT internal** dengan tim operasional SEI: alur harian mereka benar-benar terlayani, angka masuk akal dibanding persepsi lapangan. |
| G·02 | P0 | **Go-live bertahap** (Fase 9 master): internal → sebagian pengguna → umum. Pantau error rate + latency. |
| G·03 | P1 | **Alert ke tim platform**: service down, `status != GO`, forecast kedaluwarsa berjam-jam, rate limit mendekati kuota. |
| G·04 | P1 | **Dokumen pemakaian** untuk tim platform (jawab 5·08 master): URL, header, adapter, kode error, rate limit, kontak. |
| G·05 | P1 | **Runbook insiden:** service Blitz down / cold start berturut-turut / data aktual berhenti diperbarui / model version naik tapi performa turun. |
| G·06 | P2 | **Perbarui checklist master**: centang 5·05–5·08; tambah item 5·09 bila platform belum tercatat di master. |

---

## PROGRES PER FASE

- **F · Akses & Konfigurasi** 0/4
- **F · Ingesta & Normalisasi** 0/5
- **F · Tampilan Inti** 0/9
- **F · Keadaan Gagal & Kadaluarsa** 0/9
- **F · Kebijakan Data & Kuota** 0/5
- **F · Pengujian** 0/7
- **F · Roll-out & Operasional** 0/6

**Total: 0/45** — centang per item (mirip master).

## URUTAN KERJA (ketergantungan)

```
A (akses) ──▶ B (ingesta) ──▶ C (tampilan inti) ──▶ D (state gagal)
                                      │
                        E (kebijakan) ─┤
                                      ▼
                    F (uji siklus E2E + kegagalan) ──▶ G (go-live)
```

- **C·01–C·03 tidak mungkin tanpa A+B** (butuh adapter & akses).
- **D butuh C** (state menggantikan tampilan normal).
- **F·01 (siklus E2E) hanya valid setelah C+D selesai** — siklus yang belum
  menyertakan state gagal = pengujian sebagian.
- **G·02 go-live hanya setelah F·01–F·03 lulus.**

## ATURAN (adaptasi dari 7 aturan master)

1. **Monitoring hanya konsumen API** — kode algoritma tidak boleh masuk repo platform.
2. **Nama field dikunci lewat test + adapter** — ubah kontrak → edit adapter, bukan seluruh UI.
3. **Timestamp selalu `+08:00` (WITA), jangan dikonversi ke zona browser.**
4. **Riwayat forecast tidak ditimpa** — platform menyimpan per `generated_at`.
5. **Forecast dijadwalkan, tidak dihitung ulang saat paint** — baca hasil scheduler + cache.
6. **`model_version` + `generated_at` selalu tampil** — agar regresi akurasi telusur.
7. **Kegagalan harus terlihat, bukan angka basi** — setiap state gagal punya pesan (Fase D).

## KRITERIA KELUARAN

Platform monitoring menampilkan forecast pengguna akhir dari API sungguhan
(lengkap label model version + waktu pembuatan), menyembunyikan garis di luar
jendela horizon, membedakan "menunggu pengamatan" dari "data kosong",
menampilkan setiap kegagalan dengan pesan jelas tanpa angka basi, lulus siklus
E2E data nyata + simulasi kegagalan, dan siap go-live bertahap.

