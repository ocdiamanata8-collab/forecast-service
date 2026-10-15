/**
 * Adapter sisi platform monitoring — SATU-SATUNYA berkas yang perlu disentuh
 * bila nama field kontrak API berubah.
 *
 * Filosofi:
 *   response API (nama field kontrak §5.x KONTRAK_API_v1.md)
 *     → [BERKAS INI] → format internal platform ({when, value, …})
 *
 * Dashboard monitoring (dan engineer console di repo ini) meng-import fungsi
 * di bawah. Perubahan kontrak di masa depan (rename field, struktur nested)
 * hanya menyentuh berkas ini — bukan seluruh dashboard. Sebaliknya: kalau
 * Anda menulis `payload.generated_at` di mana pun di luar berkas ini, janji
 * "satu berkas" itu sudah rusak.
 *
 * Cara pakai:
 *   import { toForecast, toPerformance, toHealth, toError }
 *     from "https://<host>/static/monitoring-adapter.js";   // atau path lokal
 *   const m = toForecast(await res.json());
 *   m.points → [{ when: "2026-01-01T00:00:00+08:00", value: 222.295 }, …]
 *
 * Aturan implementasi:
 * - DEFAKTIF (never throw): field hilang/berubah → fallback null / [] / NaN.
 *   Drift kontrak TIDAK boleh membuat monitoring crash — ia cukup menampilkan
 *   data kosong; deteksinya tetap tugas audit respons mentah di dashboard dan
 *   contract-field test di server (tests/test_contract_fields.py).
 * - Angka selalu lewat Number(); daftar poin selalu diurutkan menaik per
 *   waktu, nilai non-finite dibuang (titik tanpa angka = garis patah, bukan 0
 *   yang menyesatkan di grafik).
 * - Tanpa API key / tanpa fetch di sini: berkas ini PENERJEMAH PAYLOAD murni.
 */

/** Peta field kontrak → key internal. Semua nama kontrak hidup di sini. */
const pick = (o, k) => (o && typeof o === "object" ? o[k] : undefined);
const num = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const sortPoints = (pts) =>
  pts
    .slice()
    .sort((a, b) => String(a.when).localeCompare(String(b.when)));

/**
 * POST /forecast (§5.1) & GET /forecast/latest (§5.2) → format internal.
 * Konteks (site_id, generated_at, unit, model_version) ikut diterjemahkan.
 */
export function toForecast(raw) {
  const points = (Array.isArray(pick(raw, "forecast")) ? raw.forecast : [])
    .map((p) => ({
      when: pick(p, "forecast_time") ?? null,
      value: num(pick(p, "forecast_value")),
      modelVersion: pick(p, "model_version") ?? null,
    }))
    .filter((p) => p.when && p.value !== null);
  return {
    site: pick(raw, "site_id") ?? null,
    target: pick(raw, "target") ?? null,
    unit: pick(raw, "unit") ?? null,
    timezone: pick(raw, "timezone") ?? null,
    horizon: num(pick(raw, "horizon")),
    generatedAt: pick(raw, "generated_at") ?? null,
    ts: pick(raw, "ts") ?? null,
    modelVersion: pick(raw, "model_version") ?? null,
    order: pick(raw, "order") ?? null,
    forecastId: pick(raw, "forecast_id") ?? null,
    points: sortPoints(points),
  };
}

/** GET /forecast/actual (§5.7) → deret aktual {when, value}. */
export function toActualSeries(raw) {
  const points = (Array.isArray(pick(raw, "points")) ? raw.points : [])
    .map((p) => ({
      when: pick(p, "timestamp") ?? null,
      value: num(pick(p, "actual_value")),
    }))
    .filter((p) => p.when && p.value !== null);
  return {
    site: pick(raw, "site_id") ?? null,
    from: pick(raw, "from") ?? null,
    to: pick(raw, "to") ?? null,
    source: pick(raw, "source") ?? null,
    n: num(pick(raw, "n")),
    points: sortPoints(points),
  };
}

/** GET /model/performance (§5.5) → metrik siap-pakai untuk KPI/panel. */
export function toPerformance(raw) {
  const th = pick(raw, "thresholds") ?? {};
  const horizons = (Array.isArray(pick(raw, "horizons")) ? raw.horizons : [])
    .map((h) => ({
      days: num(pick(h, "horizon")),
      mae: num(pick(h, "mae")),
      rmse: num(pick(h, "rmse")),
      mape: num(pick(h, "mape")),
      skill: num(pick(h, "skill")),
    }))
    .filter((h) => h.days !== null);
  const primary = num(pick(th, "primary_horizon")) ?? 7;
  const primaryBlock = horizons.find((h) => h.days === primary) ?? null;
  const range = pick(raw, "evaluation_range") ?? null;
  return {
    site: pick(raw, "site_id") ?? null,
    target: pick(raw, "target") ?? null,
    unit: pick(raw, "unit") ?? null,
    status: pick(raw, "status") ?? null,
    evaluatedAt: pick(raw, "evaluated_at") ?? null,
    dataRows: num(pick(raw, "data_rows")),
    nSamples: num(pick(raw, "n_samples")),
    modelVersion: pick(raw, "model_version") ?? null,
    evaluationRange: range
      ? { from: pick(range, "from") ?? null, to: pick(range, "to") ?? null }
      : null,
    failures: Array.isArray(pick(raw, "failures")) ? raw.failures : [],
    thresholds: {
      maxMae: num(pick(th, "max_mae")),
      maxMape: num(pick(th, "max_mape")),
      maxMapeLong: num(pick(th, "max_mape_long")),
      minSkill: num(pick(th, "min_skill")),
      primaryHorizon: primary,
    },
    horizons,
    /** Blok horizon utama (default 7 hari) — yang ditampilkan di KPI. */
    primary: primaryBlock,
  };
}

/** GET /model/info (§5.4) → metadata model siap-tampil. */
export function toModelInfo(raw) {
  const range = pick(raw, "training_range") ?? null;
  return {
    modelVersion: pick(raw, "model_version") ?? null,
    target: pick(raw, "target") ?? null,
    unit: pick(raw, "unit") ?? null,
    algorithm: pick(raw, "algorithm") ?? null,
    order: Array.isArray(pick(raw, "order")) ? raw.order : [],
    timezone: pick(raw, "timezone") ?? null,
    features: Array.isArray(pick(raw, "features")) ? raw.features : [],
    trainingRows: num(pick(raw, "training_rows")),
    trainedAt: pick(raw, "trained_at") ?? null,
    evaluationStatus: pick(raw, "evaluation_status") ?? null,
    deterministic: pick(raw, "deterministic") ?? null,
    trainingRange: range
      ? { from: pick(range, "from") ?? null, to: pick(range, "to") ?? null }
      : null,
  };
}

/** GET /health (§5.6) → status ringkas (probe uptime). */
export function toHealth(raw) {
  return {
    status: pick(raw, "status") ?? null,
    version: pick(raw, "service_version") ?? null,
    uptimeSeconds: num(pick(raw, "uptime_seconds")),
    modelLoaded: pick(raw, "model_loaded") ?? null,
    timestamp: pick(raw, "timestamp") ?? null,
  };
}

/** GET /forecast/history (§5.3) → ringkasan riwayat per generasi. */
export function toHistory(raw) {
  const items = (Array.isArray(pick(raw, "items")) ? raw.items : []).map(
    (it) => ({
      forecastId: pick(it, "forecast_id") ?? null,
      site: pick(it, "site_id") ?? null,
      generatedAt: pick(it, "generated_at") ?? null,
      ts: pick(it, "ts") ?? null,
      horizon: num(pick(it, "horizon")),
      modelVersion: pick(it, "model_version") ?? null,
      points: Array.isArray(pick(it, "forecast"))
        ? toForecast(it).points
        : null, // default: poin tidak dikirim (§5.3, tanpa include_points)
    })
  );
  const page = pick(raw, "page") ?? {};
  return {
    items: items.sort((a, b) =>
      String(b.generatedAt).localeCompare(String(a.generatedAt))
    ),
    page: {
      limit: num(pick(page, "limit")),
      hasMore: pick(page, "has_more") ?? null,
      nextCursor: pick(page, "next_cursor") ?? null,
    },
  };
}

/** Envelope error §3.3 → {status, code, message, requestId}. */
export function toError(raw, status) {
  const e = pick(raw, "error") ?? {};
  return {
    status: num(status) ?? null,
    code: pick(e, "code") ?? null,
    message: pick(e, "message") ?? null,
    requestId: pick(e, "request_id") ?? null,
    details: Array.isArray(pick(e, "details")) ? e.details : [],
  };
}
