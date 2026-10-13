"""Lapisan API forecasting service — komposisi, bukan logika model.

Ketentuan lapisan ini (kontrak [../../KONTRAK_API_v1.md](../../KONTRAK_API_v1.md)):

- **Tidak ada logika perhitungan forecast di sini.** Seluruh matematika ada di
  :mod:`app.engine`, :mod:`app.preprocessing`, :mod:`app.evaluation`.
- Request/response Pydantic ada di :mod:`app.schemas`.
- Penyimpanan lewat :mod:`app.storage`; autentikasi lewat :mod:`app.security`.
- Zona waktu ikut kontrak §1.5: timestamp masuk wajib offset eksplisit,
  disimpan/dibandingkan dalam UTC, diserialisasi dengan zona site.
"""
