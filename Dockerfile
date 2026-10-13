# Forecasting Service PLTS Kupang
#
# Build : docker build -t forecast-service .
# Run   : docker run -p 8000:8000 -e API_KEYS='kunci-panjang-disini' forecast-service
# Smoke : curl http://127.0.0.1:8000/api/v1/health
#
# Verifikasi dependency di laptop jauh lebih murah daripada membaca log
# deploy yang gagal — build dulu sebelum dipakai di server.

# ---------------------------------------------------------------------------
# Versi Python: 3.13, BUKAN 3.12 — ini disengaja.
# Seluruh suite (130 pytest + 13 script + golden/regresi/parity) dijalankan
# di Python 3.13.5, dan requirements.txt terkunci di versi yang memang
# tersedia wheel-nya untuk 3.13 (statsmodels==0.14.4, scipy==1.14.1).
# Menurunkan ke 3.12 tanpa menguji ulang akan memutus jaminan golden test
# dan paritas modul↔skrip.
# ---------------------------------------------------------------------------
FROM python:3.13-slim

# Label dasar untuk jejak image.
LABEL org.opencontainers.image.title="Forecasting Service PLTS Kupang" \
      org.opencontainers.image.description="Forecast irradiance harian — REST API + engineer dashboard"

WORKDIR /srv

# Salin manifest dependency dulu → lapisan ini ter-cache selama requirements
# tidak berubah; perubahan kode aplikasi tidak memicu install ulang.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root: service tidak butuh hak tulis apa pun selain direktori kerja
# (storage in-memory; report.json/actuals.csv hanya dibaca).
RUN useradd --create-home --shell /usr/sbin/nologin app

# Semua artefak runtime — salinan eksplisit, bukan `COPY . .`:
#   app/                    kode layanan (engine, preprocessing, service, api)
#   model/VERSION           sumber kebenaran versi (wajib — startup menagihnya)
#   model/artifacts.json    urutan fitur & skala (wajib — API tidak menebak)
#   templates/              dashboard engineer di GET /  (wajib)
#   data/actuals.csv        data aktual per titik (wajib — /forecast/actual)
#   data/evaluation/…       laporan backtest untuk /model/performance (opsional,
#                           diikutkan bila ada agar status evaluasi tampil)
#   scripts/                prepare_actuals.py / run_evaluation.py / validate_e2e.py
#
# model/model.pkl TIDAK ikut image — dikecualikan oleh .dockerignore (alasan
# sama dengan .gitignore): runtime TIDAK pernah memuatnya, engine refit per
# request. Regenerasi bila perlu:  python scripts/export_model.py
#
# templates/ dan data/ MASING-MASING PERNAH terlewat — akibatnya GET / 404 dan
# /forecast/actual 503. Jangan hapus baris COPY di bawah tanpa membaca
# app/main.py, app/actuals.py, app/api/model.py.
COPY --chown=app:app app/ ./app/
COPY --chown=app:app model/ ./model/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app data/ ./data/
COPY --chown=app:app scripts/ ./scripts/

# API key HANYA dari environment — tidak ada fallback key di kode (kebijakan
# repo hygiene: kebocoran API key = masalah termahal di tahap ini).
# Tanpa -e API_KEYS, endpoint ber-key membalas 401 E_UNAUTHORIZED (by design);
# /api/v1/health tetap terbuka untuk healthcheck.
ENV HOST=0.0.0.0 \
    PORT=8000

# Jalankan sebagai user non-root.
USER app

EXPOSE 8000

# Healthcheck memakai endpoint tanpa API key (kontrak §5.6).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r=urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4); \
        sys.exit(0 if r.status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
