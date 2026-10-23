# 5·07 — PERILAKU BILA API TIDAK TERSEDIA / FORECAST KEDALUARSA

**Fase 5 · F5·07 · SEI Forecasting Service · 16 Okt 2026**
Status: **DIPUTUSKAN** — angka berasal dari kode yang sudah jalan
(`static/monitoring-adapter.js`, `KONTRAK_API_v1.md` §1.4/§5, `app/scheduler.py`).

---

## TIGA JAM BERBEDA — sumber kebenaran masing-masing

| Jam | Sumber | Dipakai untuk |
|---|---|---|
| `ts` | payload `latest` | awal jendela validitas forecast |
| `validUntil` | adapter = `ts + horizon hari` (offset `ts`, bukan Z) | batas akhir garis forecast |
| `generated_at` | payload + `Cache-Control: max-age=60` | umur data di layar |

Jangan mencampur: **`validUntil` menentukan isi garis; `generated_at` menentukan
apakah isi itu masih boleh dipercaya.**

---

## ATURAN 1 — Forecast berlaku selama `now ≤ validUntil`

`validUntil` = `ts + horizon hari` (default platform: 7 hari; batas kontrak: 30).

| Kondisi | Garis forecast | Kartu / label |
|---|---|---|
| `now ≤ validUntil` dan `inHorizon` per titik | **digambar** | badge **BERLAKU** |
| `now > validUntil` | **dihapus total** (garis, band, tabel, tooltip — bukan dipertahankan redup) | badge **KEDALUARSA** + "forecast kedaluwarsa — menunggu regenerasi berikutnya" |

`now` = jam klien + `timezone` dari payload (`Asia/Makassar`, +08:00) — **jangan
zona browser**.

**Syarat amplifikasi (lanjutan):** lapisan platform menjaga
`generated_at` ≈ `ts` (keduanya saat regenerasi). Bila `now - generated_at > 6 jam`
(= `ts` tertinggal 6 jam, di luar cadangan scheduler per jam × ambang kewarasan),
anggap **`ts` basi** → perlakukan seperti lewat `validUntil` (garis disembunyikan)
**meski** `now ≤ validUntil`. Forecast berbasis data 6 jam lalu sudah tidak
mewakili hari yang sama.

## ATURAN 2 — Service tidak tersedia (fetch gagal / non-2xx)

Tiga durasi menentukan tingkat pesan — **semua level menghapus nilai, tidak pernah
mempertahankan angka/garis lama:**

| Durasi | Kondisi | Yang ditampilkan |
|---|---|---|
| `< 2 menit` | cold start / blip | **state loading**, tampilkan cache yang umurnya **< 1 jam** dengan label `terakhir diperbarui HH:MM +08:00` — **tanpa** badge BERLAKU (hanya "data tersimpan"); auto-retry **5s → 15s → 30s** |
| `2–60 menit` | outage singkat / tidur free tier | **state "layanan sedang gangguan"**: **hapus garis forecast**; cache hanya dipertahankan bila `now - generated_at ≤ 1 jam`, digelapkan + label `data terakhir HH:MM +08:00 · mungkin sudah kedaluwarsa`; tanpa badge BERLAKU, tanpa KPI GO; auto-retry **60s** |
| `> 60 menit` | outage panjang | **state "tidak ada data forecast terbaru"**: **hapus semua nilai dan garis** (ikut kontrak §4.2: bila usang > 1 jam, respons dianggap stale dan tidak ditampilkan); tombol **coba ulang manual**; alert tim platform menyala |

Batas 1 jam selaras dengan: scheduler per jam (forecast segar selambatnya 1 jam)
dan §1.4 (cache 60 detik) — **TTL cache platform = 1 jam, tidak lebih.**

## ATURAN 3 — Pesan (teks persis)

| Kode / situasi | Pesan pengguna |
|---|---|
| Outage `< 2 mnt` | `Menyambung ulang ke layanan forecast…` |
| Outage `2–60 mnt` | `Layanan forecast sedang gangguan — menampilkan data terakhir terakhir dari HH:MM +08:00. Garis forecast tidak ditampilkan.` |
| Outage `> 60 mnt` | `Layanan forecast sedang gangguan dan data terakhir sudah usang. Tidak ada forecast ditampilkan. Coba lagi nanti.` |
| `now > validUntil` | `Forecast kedaluwarsa (berlaku s.d. DD MMM YYYY). Menunggu regenerasi.` |
| `404 E_NO_FORECAST` | `Belum ada forecast — scheduler belum berjalan untuk site ini.` |
| `401/403` | `Akses ditolak. Hubungi admin platform.` |
| `429` | `Terlalu banyak permintaan. Coba lagi dalam ${Retry-After} detik.` |
| `503` / cold start | `Layanan sedang menyala ulang…` |
| Selalu disertai | `Terakhir diperbarui HH:MM +08:00` + `model vX · horizon N hari` |

## ATURAN 4 — Rekap "kapan garis forecast disembunyikan"

Garis forecast **tidak boleh** muncul bila salah satu terpenuhi:

1. `now > validUntil` (melewati `ts + horizon hari`), **atau**
2. `now - generated_at > 6 jam` (`ts`/data sumber basi), **atau**
3. Service tidak tersedia **≥ 2 menit** (outage tanpa cache), **atau**
4. Cache terpakai dan `now - generated_at > 1 jam` (data stale menurut §4.2), **atau**
5. Titik individual `inHorizon = false` (filter per titik — titik ≠ seluruh garis).

Yang **tetap tampil** saat garis tersembunyi: kartu analisis nilai terakhir **dengan
label `terakhir diperbarui`** + pesan kondisi — selama `now - generated_at ≤ 1 jam`.
Lewat itu: **bersih total, plus pesan.**

## TEST (menambah guard yang sudah ada — 178 lulus saat ini)

- `validUntil` terhitung dari `ts + horizon` dengan offset `ts` (bukan `toISOString` Z).
- Fase outage (2/10/90 menit): pill, pesan level, garis dihapus, nilai direset — tidak ada angka basi / badge BERLAKU.
- `now > validUntil`: garis hilang, badge KEDALUARSA muncul, tabel & tooltip ikut.
- Cache `generated_at` 3 jam yang **gagal di-refresh**: dianggap stale walau dalam outage singkat.
- Retry schedule (5/15/30s → 60s) dan pemulihan otomatis setelah service hidup.
