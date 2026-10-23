"""Uji halaman dashboard engineer.

    pytest tests/test_dashboard.py -q

Prinsip halaman (instruksi): halaman ini adalah KONSUMEN API, sama seperti
platform monitoring — bukan tuan rumah data. Karena itu test-nya memeriksa
dua arah:

A. Halaman terlayani dan tersambung ke API yang benar.
B. Halaman TIDAK menyembunyikan logika/data di dirinya (kalau ya, ia tidak
   lagi menguji API dan perubahan kontrak tidak akan ketahuan).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Mode script tidak memuat conftest — matikan scheduler supaya state test
# deterministik (store kosong; ARIMA fit tidak jalan tiap TestClient).
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------
def _html() -> str:
    with TestClient(create_app()) as c:
        return c.get("/").text


def test_root_serves_html_without_api_key():
    """/ = halaman dashboard, terbuka tanpa key (engineer membuka dari browser)."""
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.status_code != 401, "halaman engineer tidak butuh API key"


def test_role_document_exists_and_states_four_functions():
    """Peran dikunci tertulis sebelum desain (templates/ROLE.md)."""
    role = Path(__file__).resolve().parents[1] / "templates" / "ROLE.md"
    assert role.exists(), "templates/ROLE.md wajib ada — peran harus tertulis"
    text = role.read_text(encoding="utf-8")
    for function in ("Memilih site", "Mengubah horizon", "forecast manual", "data aktual"):
        assert function.lower() in text.lower(), f"fungsi '{function}' tidak disebut ROLE.md"


def test_role_document_forbids_mixing_with_end_user_monitoring():
    """Batas keras: forecast pengguna akhir = platform monitoring, bukan halaman ini."""
    text = (Path(__file__).resolve().parents[1] / "templates" / "ROLE.md").read_text(encoding="utf-8")
    assert "pengguna akhir" in text
    assert "monitoring" in text.lower()
    assert "TIDAK" in text or "jangan" in text.lower(), \
        "ROLE.md harus menyatakan halaman ini BUKAN untuk pengguna akhir"


def test_dashboard_shows_role_banner():
    """Banner peran harus tampil di halaman, agar pembaca langsung paham batasnya."""
    html = _html()
    assert "templates/ROLE.md" in html, "banner peran tidak ada"
    assert "pengguna akhir" in html
    assert "monitoring" in html.lower()


def test_function_1_site_selector():
    """Fungsi #1: memilih site — kontrol ada dan semua panggilan memakainya."""
    html = _html()
    assert 'id="site"' in html, "kontrol pemilih site tidak ada"
    # Kebijakan: pemilihan site harus lewat selector, bukan di-hardcode.
    # Label contoh request memakai placeholder `<SITE_ID>` — bukan literal.
    hardcoded = re.findall(r'\?site_id=PLTS_KUPANG', html)
    assert not hardcoded, \
        f"site di-hardcode dalam {len(hardcoded)} panggilan — pemilihan site harus dari kontrol"
    assert "encodeURIComponent" in html, "site harus di-encode, bukan disisipkan literal"


def test_function_2_horizon_control():
    """Fungsi #2: mengubah horizon — kontrol ada dan dikirim ke POST."""
    html = _html()
    assert 'id="horizon"' in html, "kontrol horizon tidak ada"
    assert 'id="horizon" min="1" max="30"' in html.replace("\n", " "), \
        "horizon harus dibatasi 1..30 sesuai kontrak"
    assert "horizon()" in html and "horizon," in html, \
        "nilai horizon tidak dikirim ke request"


def test_function_3_manual_forecast_uses_post_endpoint():
    """Fungsi #3: menjalankan forecast manual lewat POST /api/v1/forecast."""
    html = _html()
    assert 'id="runBtn"' in html, "tombol jalankan forecast tidak ada"
    assert "/api/v1/forecast" in html and 'method: "POST"' in html.replace("\n", " "), \
        "tidak memanggil POST /api/v1/forecast"
    # History harus disediakan engineer (textarea), bukan history internal.
    assert 'id="historyTa"' in html, "area input history engineer tidak ada"


def test_function_4_comparison_with_actual_uses_performance():
    """Fungsi #4: membandingkan dengan data aktual lewat /model/performance."""
    html = _html()
    assert 'id="evalBtn"' in html, "tombol evaluasi tidak ada"
    assert "/api/v1/model/performance" in html, \
        "perbandingan harus memakai endpoint performance (aktual-vs-forecast)"
    # Menampilkan metrik perbandingan.
    for metric in ("MAE", "MAPE", "RMSE", "skill"):
        assert metric in html, f"metrik {metric} tidak ditampilkan"


def test_dashboard_declares_actual_vs_forecast_limitation():
    """Keterbatasan yang tersisa harus jujur; keterbatasan lama sudah dicabut.

    Dulu: "perbandingan per-titik BELUM tersedia dari API". Kini endpoint
    `/api/v1/forecast/actual` ada — teks lama diganti, dan keterbatasan yang
    benar (rentang data aktual 2023-2025, tanpa interpolasi) disebut.
    """
    html = _html()
    assert "perbandingan per-titik (forecast vs aktual di tanggal yang sama)" in html \
        or "per-titik (forecast vs aktual di tanggal yang sama)" in html, \
        "panel §3 harus menjelaskan perbandingan per-titik memakai endpoint aktual"
    assert "GET /api/v1/forecast/actual" in html, "endpoint actual harus disebut"
    # Keterbatasan lama tidak boleh mengklaim hal yang kini bisa.
    assert "belum tersedia dari API" not in html, \
        "klaim basi: perbandingan per-titik kini tersedia lewat /forecast/actual"


def test_dashboard_has_offline_svg_comparison_chart():
    """Grafik actual vs forecast: SVG inline, tanpa library/CDN."""
    html = _html()
    assert 'id="comparisonFigure"' in html, "panel grafik tidak ada"
    assert 'id="comparisonChart"' in html and "<svg" in html, "SVG grafik tidak ada"
    # Offline penuh: tidak boleh menambah sumber eksternal.
    assert 'src="http' not in html and 'href="http' not in html, \
        "sumber eksternal terdeteksi — halaman harus offline"
    assert "chart.js" not in html.lower() and "d3." not in html.lower(), \
        "library grafik eksternal terdeteksi"


def test_dashboard_chart_uses_actual_api_and_no_csv():
    """Grafik mengambil actual dari API, tidak pernah dari CSV/FS."""
    html = _html()
    assert "/api/v1/forecast/actual" in html, "grafik tidak memanggil endpoint actual"
    assert "loadActualsAndDraw" in html, "fungsi pengambilan actual tidak ada"
    for banned in ("read_csv", "B1_daily_master", ".csv'", "fs."):
        assert banned not in html, f"grafik menyentuh CSV/FS: {banned}"


def test_dashboard_chart_labels_error_as_absolute_not_confidence():
    """Bayangan error = selisih absolut, bukan interval ketidakpastian."""
    html = _html()
    assert "Selisih |aktual−forecast|" in html or "error absolut" in html.lower(), \
        "legend harus menjelaskan band sebagai selisih absolut"
    assert "confidence" not in html.lower() and "interval ketidakpastian (terklaim)" not in html, \
        "jangan menyebut band sebagai confidence/interval"
    # Tabel accessible tetap ada (table view twin grafik).
    assert 'id="comparisonTable"' in html, "tabel accessible grafik tidak ada"


def test_dashboard_chart_has_legend_and_crosshair():
    """≥2 series wajib legend; crosshair+tooltip wajib (skill dataviz)."""
    html = _html()
    assert 'class="chart-legend"' in html, "legend tidak ada"
    assert "actual-line" in html and "forecast-line" in html, "legend dua series tidak ada"
    assert "chartCrosshair" in html and "chartTooltip" in html, "crosshair/tooltip tidak ada"
    assert "ArrowRight" in html, "navigasi keyboard crosshair tidak ada"


def test_dashboard_is_api_consumer_calls_documented_endpoints():
    """JS harus memanggil endpoint kontrak — inilah yang menguji API."""
    with TestClient(create_app()) as c:
        html = c.get("/").text
    for endpoint in (
        "/api/v1/forecast/latest",   # diminta instruksi
        "/api/v1/health",
        "/api/v1/model/info",
        "/api/v1/model/performance",
        "/api/v1/forecast",          # fungsi #3 — run manual (POST)
    ):
        assert endpoint in html, f"JS tidak memanggil {endpoint} — bukan konsumen API"
    # Memanggil lewat fetch, bukan menanam URL sebagai link statis saja.
    assert "fetch(" in html, "tanpa fetch() halaman tidak memanggil API"


def test_dashboard_carries_context_field_audit():
    """Audit JS harus memeriksa field konteks (site_id/generated_at/unit/model_version)."""
    with TestClient(create_app()) as c:
        html = c.get("/").text
    m = re.search(r'CTX_FIELDS\s*=\s*\[([^\]]+)\]', html)
    assert m, "CTX_FIELDS tidak ditemukan — audit konteks hilang"
    for field in ("site_id", "generated_at", "unit", "model_version"):
        assert f'"{field}"' in m.group(1), f"{field} tidak diaudit"


def test_chart_labels_forecast_meta_and_hides_out_of_horizon():
    """Grafik pemantauan: (a) label meta model+generated_at, (b) titik di luar
    jendela horizon disembunyikan dari garis — permintaan integrasi monitoring.
    """
    with TestClient(create_app()) as c:
        html = c.get("/").text
    # (a) elemen label meta ada dan diisi dari adapter (textContent).
    assert 'id="chartMeta"' in html, "elemen label meta grafik tidak ada"
    assert "dibuat" in html and "horizon" in html, \
        "label meta tidak memuat waktu pembuatan + horizon"
    # (b) garis forecast memakai jendela inHorizon, bukan semua titik.
    assert "inHorizon" in html, \
        "grafik tidak memakai guard jendela horizon (inHorizon) — titik di luar horizon ikut digambar"
    assert "fSegments" in html, \
        "garis forecast tidak dipecah pada batas horizon (bisa menjembatani titik tersembunyi)"
    # (c) forecast tersimpan (scheduler/latest) juga memicu grafik pembanding.
    load_latest = html[html.index("async function loadLatest"):html.index("$(\"latestBtn\")")]
    assert "loadActualsAndDraw" in load_latest, \
        "loadLatest tidak mengisi grafik — forecast tersimpan tidak tampil sebagai garis pembanding"


def test_dashboard_does_not_embed_forecast_data():
    """Halaman tidak boleh menyimpan angka forecast — datanya harus dari API.

    Kalau ada angka forecast di HTML, halaman bisa tampak 'berhasil' walau API
    rusak, dan perubahan kontrak tidak ketahuan.
    """
    with TestClient(create_app()) as c:
        html = c.get("/").text
    # Tidak ada payload forecast yang dibakar.
    assert '"forecast": [' not in html, "payload forecast dibakar ke HTML"
    assert not re.search(r'"forecast_value"\s*:\s*\d', html), \
        "angka forecast_value tercantum di HTML (harusnya datang dari fetch)"
    # Tidak ada key yang dibakar.
    assert "dev-key-change-me" not in html, "API key bocor ke HTML"


def test_dashboard_not_exposed_in_openapi():
    """Swagger harus hanya menampilkan endpoint kontrak (6), bukan halaman UI."""
    with TestClient(create_app()) as c:
        paths = c.get("/openapi.json").json()["paths"]
    assert "/" not in paths, "halaman UI bocor ke OpenAPI — Swagger jadi berantakan"
    assert "/docs" not in paths
    expected = {
        "/api/v1/forecast",
        "/api/v1/forecast/actual",
        "/api/v1/forecast/latest",
        "/api/v1/forecast/history",
        "/api/v1/model/info",
        "/api/v1/model/performance",
        "/api/v1/health",
    }
    assert set(paths) == expected, set(paths)


def test_docs_and_redoc_available():
    """/docs & /redoc tetap tersedia dan menyajikan Swagger/ReDoc yang benar."""
    with TestClient(create_app()) as c:
        docs = c.get("/docs")
        assert docs.status_code == 200
        assert "swagger" in docs.text.lower(), "bukan halaman Swagger"
        # Swagger membaca spesifikasi dari /openapi.json (bukan dari disk).
        assert "/openapi.json" in docs.text

        redoc = c.get("/redoc")
        assert redoc.status_code == 200
        assert "redoc" in redoc.text.lower()


def test_dashboard_links_to_docs():
    """Dashboard harus menautkan Swagger agar engineer mudah melompat ke sana."""
    with TestClient(create_app()) as c:
        html = c.get("/").text
    assert 'href="/docs"' in html
    assert 'href="/redoc"' in html
    assert 'href="/openapi.json"' in html


def test_dashboard_has_error_rendering_for_api_errors():
    """Halaman harus bisa menampilkan envelope error API (bukan stack trace)."""
    with TestClient(create_app()) as c:
        html = c.get("/").text
    assert '"error"' in html or "error.code" in html or "e.code" in html, \
        "JS tidak membaca error.code dari envelope"
    # Tidak boleh ada upaya mem-parse stack trace.
    assert "Traceback" not in html


def test_dashboard_shows_service_unavailable_not_stale_values():
    """Uji E2E outage: saat service mati, dashboard harus menampilkan
    keadaan itu dengan JELAS — bukan angka/sertifikat hijau basi.

    Tiga celah yang pernah terjadi (direproduksi saat uji siklus E2E):
    1. api() melempar → cabang else tak pernah jalan → pill lama tertinggal.
    2. showErr status 0 dianggap sukses (status < 400) → error disembunyikan.
    3. refreshSummary tak mereset kpiStatus → "GO" basi tertinggal.
    """
    with TestClient(create_app()) as c:
        html = c.get("/").text
    # 1. api() non-melempar: fetch gagal → {status:0, envelope transport}.
    assert "catch (_)" in html and '"E_SERVICE_UNAVAILABLE"' in html, \
        "api() harus menangkap fetch gagal dan mengembalikan status 0"
    assert "service tidak terjangkau" in html, \
        "pesan outage harus eksplisit 'service tidak terjangkau'"
    # 2. showErr TIDAK menganggap status 0 sebagai sukses.
    assert "r.status >= 200 && r.status < 400" in html, \
        "showErr harus hanya membersihkan error pada respons sukses 2xx/3xx"
    # 3. kpiStatus ikut di-reset saat performance gagal.
    assert "Performance tidak terbaca" in html, \
        "kpiStatus harus di-reset saat performance gagal (bukan GO basi)"
    # 4. health tidak meninggalkan versi/uptime basi saat gagal.
    rh = html[html.index("async function refreshHealth"):html.index("/* ---------------- Grafik actual vs forecast")]
    assert rh.count("–") >= 2, "refreshHealth harus mengisi '–' pada hVer/hUp saat gagal"
    # 5. grafik + meta basi dibersihkan saat service tidak terjangkau.
    assert "clearComparisonState" in html, \
        "helper pembersih grafik+meta saat outage tidak ada"


def test_root_error_when_template_missing(tmp_path, monkeypatch):
    """Bila template hilang → 404 envelope §3.3, bukan 500 / stack trace."""
    import app.main as main_mod

    # Arahkan TEMPLATE_DIR ke folder kosong: index.html tidak ada.
    monkeypatch.setattr(main_mod, "TEMPLATE_DIR", tmp_path)
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 404, r.status_code
        payload = r.json()
        assert payload["error"]["code"] == "E_NOT_FOUND", payload
        assert "template missing" in payload["error"]["message"]
        assert "Traceback" not in r.text


def test_dashboard_static_asset_no_side_effects():
    """GET / tidak membuat file / tidak memanggil engine (konsumen murni)."""
    with TestClient(create_app()) as c:
        before = {p.name for p in Path(".").iterdir()}
        r = c.get("/")
        after = {p.name for p in Path(".").iterdir()}
    assert r.status_code == 200
    assert before == after, "GET / membuat/menghapus file"


# ---------------------------------------------------------------------------
# Ringkasan KPI + status endpoint (panel atas, terisi otomatis)
# ---------------------------------------------------------------------------
def test_summary_panel_exists_and_auto_loads():
    """Panel ringkasan ada dan terisi otomatis (tanpa klik)."""
    html = _html()
    assert 'id="summaryPanel"' in html, "panel ringkasan tidak ada"
    assert "refreshSummary()" in html, "fungsi refreshSummary tidak ada"
    assert "renderEndpointGrid()" in html, "grid endpoint tidak dirender"
    # Dipanggil saat load, bukan menunggu tombol.
    assert "refreshSummary();" in html.replace("\n", " "), \
        "refreshSummary tidak dipanggil otomatis"


def test_summary_kpi_fields_present():
    """Lima KPI yang diminta pengguna ada dan punya elemen masing-masing."""
    html = _html()
    for kpi_id in ("kpiLastRun", "kpiModel", "kpiMae", "kpiMape", "kpiRows"):
        assert f'id="{kpi_id}"' in html, f"KPI {kpi_id} tidak ada"
    # Label sesuai permintaan pengguna.
    for label in ("Last run", "Model", "MAE", "MAPE", "Data rows"):
        assert f"<b>{label}</b>" in html, f"label KPI '{label}' tidak ada"


def test_summary_kpi_reads_training_rows_from_api():
    """Data rows diambil dari field training_rows API, bukan angka karangan."""
    html = _html()
    assert "training_rows" in html, \
        "KPI Data rows harus membaca field training_rows dari /model/info"
    # Jangan ada angka 1096 yang dibakar (sumber kebenaran = API).
    assert "1096" not in html, \
        "angka 1096 dibakar ke HTML — harus datang dari API, bukan hardcode"


def test_summary_last_run_uses_report_timestamp_not_request_time():
    """Last run harus memakai evaluated_at (waktu backtest), bukan waktu request.

    evaluated_at sudah diperbaiki di backend: sumbernya generated_at dari
    report.json (waktu backtest dibuat), bukan site_now() (waktu request).
    """
    html = _html()
    assert "evaluated_at" in html, \
        "KPI Last run harus membaca evaluated_at dari /model/performance"
    # fmtStamp harus ada dan TIDAK mengonversi zona — offset asli dipertahankan.
    assert "function fmtStamp" in html, "fmtStamp tidak ditemukan"
    fmt_body = re.search(r'function fmtStamp.*?\n\}', html, re.S)
    assert fmt_body, "body fmtStamp tidak ditemukan"
    for banned in ("toLocaleString", "toISOString", "getTimezoneOffset"):
        assert banned not in fmt_body.group(0), \
            f"fmtStamp memakai {banned} — zona tidak boleh dikonversi ke browser"


def test_endpoint_grid_lists_all_seven_endpoints():
    """Grid endpoint menampilkan 7 endpoint + status + tombol uji."""
    html = _html()
    assert 'id="endpointGrid"' in html, "grid endpoint tidak ada"
    assert 'id="probeAllBtn"' in html, "tombol uji semua tidak ada"
    assert "Uji semua endpoint" in html, "teks tombol uji semua tidak ada"
    # Daftar endpoint di array ENDPOINTS.
    m = re.search(r'const ENDPOINTS\s*=\s*\[(.*?)\];', html, re.S)
    assert m, "array ENDPOINTS tidak ditemukan"
    for path in ("/api/v1/health", "/api/v1/forecast/latest",
                 "/api/v1/forecast/history", "/api/v1/forecast/actual",
                 "/api/v1/model/info", "/api/v1/model/performance",
                 "/api/v1/forecast"):
        assert path in m.group(1), f"endpoint {path} tidak ada di ENDPOINTS"


def test_endpoint_grid_health_is_auto_probed():
    """health dipanggil otomatis (tanpa key) dan statusnya dicatat di grid."""
    html = _html()
    assert "setEndpointStatus(\"health\"" in html.replace("\n", " "), \
        "status health harus dicatat di grid otomatis"
    assert "dipanggil otomatis" in html, "label health auto harus jelas"


def test_endpoint_grid_uses_textcontent_not_innerhtml_for_data():
    """Semua nilai API di-insert via textContent — data tak terpercaya."""
    html = _html()
    # Cek fungsi probeEndpoint dan setEndpointStatus tidak memakai innerHTML.
    probe = re.search(r'(?:async\s+)?function probeEndpoint.*?\n\}', html, re.S)
    assert probe, "probeEndpoint tidak ditemukan"
    assert "innerHTML" not in probe.group(0), "probeEndpoint memakai innerHTML"
    set_ep = re.search(r'function setEndpointStatus.*?\n\}', html, re.S)
    assert set_ep, "setEndpointStatus tidak ditemukan"
    assert "innerHTML" not in set_ep.group(0), "setEndpointStatus memakai innerHTML"


def test_endpoint_grid_posts_sample_forecast():
    """Tombol uji POST /forecast mengirim request contoh yang valid."""
    html = _html()
    assert "sampleForecastBody" in html, "sampleForecastBody tidak ada"
    # History contoh minimal 30 titik.
    assert "for (let i = 0; i < 60" in html, "history contoh harus minimal 60 titik"
    assert "site_id: site()" in html, "site_id harus dari pemilih site, bukan hardcode"


# ---------------------------------------------------------------------------
# Tombol salin response JSON (untuk tim monitoring menulis parser)
# ---------------------------------------------------------------------------
def test_copy_response_json_button_per_endpoint():
    """Tiap endpoint di grid punya tombol 'Salin JSON'."""
    html = _html()
    assert 'dataset.epCopy = ep.key' in html.replace("\n", " "), \
        "dataset.epCopy tidak diisi — tombol salin tidak terhubung ke endpoint"
    assert 'className = "ghost ep-copy"' in html, "tombol salin tidak dibuat"
    assert '"Salin JSON"' in html, "label tombol 'Salin JSON' tidak ada"
    # Tombol disabled sampai ada response yang bisa disalin.
    assert "copyBtn.disabled = true" in html, "tombol salin harus disabled saat mulai"
    assert "copyBtn.disabled = false" in html, "tombol salin harus aktif setelah uji"


def test_copy_stores_raw_response_for_each_endpoint():
    """Response mentah disimpan per endpoint — termasuk envelope error."""
    html = _html()
    assert "lastRawResponse" in html, "penyimpanan respons mentah tidak ada"
    # Simpan apa adanya, bukan hasil transformasi.
    assert "JSON.stringify(r.data, null, 2)" in html.replace("\n", " "), \
        "response harus disimpan persis apa adanya"
    # Network error pun bisa disalin (bukan diam).
    assert "NETWORK_ERROR" in html, "network error harus bisa disalin"


def test_copy_text_has_clipboard_fallback():
    """Clipboard API punya fallback execCommand — jangan biarkan tombol diam."""
    html = _html()
    assert "function copyText" in html, "copyText tidak ada"
    assert "navigator.clipboard" in html, "clipboard API tidak dipakai"
    assert "execCommand" in html, "fallback execCommand tidak ada"
    # Feedback visual: tombol menampilkan hasil salin.
    assert "tersalin" in html, "feedback visual 'tersalin' tidak ada"
    assert "gagal" in html, "feedback kegagalan salin tidak ada"


def test_manual_section_still_has_copy_response():
    """Tombol 'Salin response' di §2 (manual run) tetap ada — tidak dihapus."""
    html = _html()
    assert 'id="copyRes"' in html, "tombol Salin response di §2 hilang"
    assert "Salin response" in html, "label 'Salin response' hilang"
    assert 'id="copyCurl"' in html, "tombol Salin sebagai cURL hilang"
    assert 'id="copyReq"' in html, "tombol Salin request hilang"


def test_copy_endpoints_do_not_expose_api_key():
    """Response yang disalin TIDAK mengandung API key — jangan bocorkan ke clipboard."""
    html = _html()
    # Semua pemanggilan copyText hanya menerima JSON response + label —
    # tidak ada pemanggilan yang meneruskan headers.
    calls = re.findall(r'copyText\(([^)]*)\)', html)
    assert calls, "tidak ada pemanggilan copyText"
    for c in calls:
        assert "headers" not in c and "apiKey" not in c and "X-API-Key" not in c, \
            f"copyText menerima data sensitif: {c}"
    # Var yang disalin berasal dari response, bukan objek res.headers.
    assert "res.headers" not in " ".join(calls), "copyText menerima res.headers"


# ---------------------------------------------------------------------------
# Mode script
# ---------------------------------------------------------------------------
def main() -> int:
    print("\n[1] HALAMAN TERLAYANI TANPA API KEY")
    with TestClient(create_app()) as c:
        r = c.get("/")
        check(r.status_code == 200, f"GET / -> {r.status_code}")
        check(r.headers["content-type"].startswith("text/html"),
              f"content-type={r.headers['content-type']}")
        check(len(r.content) > 1000, f"ukuran {len(r.content)} bytes")
        html = r.text

    print("\n[2] KONSUMEN API — memanggil endpoint kontrak")
    for ep in ("/api/v1/forecast/latest", "/api/v1/health",
               "/api/v1/model/info", "/api/v1/model/performance",
               "/api/v1/forecast"):  # fungsi #3 — run manual (POST)
        check(ep in html, f"memanggil {ep}")
    check("fetch(" in html, "memakai fetch() — bukan link statis")
    check("X-API-Key" in html, "mengirim X-API-Key dari localStorage")

    print("\n[2b] EMPAT FUNGSI PERAN (templates/ROLE.md)")
    check('id="site"' in html, "fungsi #1 — pemilih site ada")
    check('id="horizon" min="1" max="30"' in html.replace("\n", " "),
          "fungsi #2 — horizon 1..30 ada")
    check('method: "POST"' in html.replace("\n", " "), "fungsi #3 — POST forecast manual")
    check("/api/v1/model/performance" in html, "fungsi #4 — perbandingan aktual")
    for metric in ("MAE", "MAPE", "RMSE", "skill"):
        check(metric in html, f"fungsi #4 — metrik {metric} ditampilkan")
    check("templates/ROLE.md" in html and "pengguna akhir" in html,
          "banner peran & batas monitoring pengguna akhir tampil")
    check('?site_id=PLTS_KUPANG' not in html, "site tidak di-hardcode dalam panggilan")

    print("\n[3] AUDIT KONTEKS — field konteks diperiksa JS")
    m = re.search(r'CTX_FIELDS\s*=\s*\[([^\]]+)\]', html)
    check(bool(m), "CTX_FIELDS ditemukan")
    if m:
        for f in ("site_id", "generated_at", "unit", "model_version"):
            check(f'"{f}"' in m.group(1), f"menaudit {f}")
    check("auditForecastPayload" in html and "auditHealth" in html, "fungsi audit ada")
    check("Asia/Makassar" in html or '"+08:00"' in html,
          "audit memeriksa offset zona (§1.5)")

    print("\n[4] BUKAN TUAN DATA — tidak ada angka/key yang dibakar")
    check('"forecast": [' not in html, "payload forecast tidak dibakar")
    check(not re.search(r'"forecast_value"\s*:\s*\d', html),
          "forecast_value berupa angka tidak ada di HTML")
    check("dev-key-change-me" not in html, "API key tidak bocor ke HTML")
    check("Traceback" not in html, "tanpa potongan traceback")

    print("\n[5] OPENAPI / SWAGGER")
    with TestClient(create_app()) as c:
        paths = set(c.get("/openapi.json").json()["paths"])
        docs = c.get("/docs")
        redoc = c.get("/redoc")
    check("/" not in paths and "/docs" not in paths,
          "halaman UI tidak bocor ke OpenAPI")
    check(len(paths) == 7,
          f"7 endpoint kontrak di Swagger — 6 lama + actual non-breaking "
          f"(dapat {len(paths)}: {sorted(paths)})")
    check(docs.status_code == 200 and "swagger" in docs.text.lower(), "GET /docs -> Swagger")
    check(redoc.status_code == 200, "GET /redoc tersedia")
    check('href="/docs"' in html and 'href="/openapi.json"' in html,
          "dashboard menautkan Swagger/OpenAPI")

    print("\n[6] SAMPINGAN")
    with TestClient(create_app()) as c:
        before = {p.name for p in Path(".").iterdir()}
        c.get("/")
        after = {p.name for p in Path(".").iterdir()}
    check(before == after, "GET / tidak membuat file")

    print("\n" + "=" * 64)
    if FAILURES:
        print(f"GAGAL: {len(FAILURES)} pemeriksaan")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("DASHBOARD KONSUMEN API — audit kontrak siap dijalankan tiap dibuka")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
