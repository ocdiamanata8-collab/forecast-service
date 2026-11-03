/**
 * Ingesta & normalisasi platform monitoring — Fase B checklist pengguna akhir.
 *
 * Berpasangan dengan `monitoring-adapter.js` (terjemahan field kontrak).
 * Berkas ini menangani:
 *   B·01 — fetch health (tanpa key) + latest + history + performance (dengan key)
 *   B·02 — normalisasi lewat adapter (bukan field kontrak mentah di UI)
 *   B·03 — cache per site + generated_at, TTL + revalidate background
 *   B·04 — inHorizon / validUntil hanya dari adapter (tidak dihitung ulang)
 *   B·05 — modelVersion & generatedAt dipertahankan hingga layer tampilan
 *
 * Zero dependencies; `fetch` dapat dioverride (Node 18+, browser, worker).
 * API key tidak pernah di-log — hanya lewat header X-API-Key.
 */

import {
  toForecast,
  toHistory,
  toPerformance,
  toHealth,
  toError,
} from "./monitoring-adapter.js";

/** Titik tengah rekomendasi checklist (5–15 menit). */
export const DEFAULT_CACHE_TTL_MS = 10 * 60 * 1000;

export const DEFAULT_HISTORY_LIMIT = 50;

const MIN_CACHE_TTL_MS = 5 * 60 * 1000;
const MAX_CACHE_TTL_MS = 15 * 60 * 1000;

/**
 * @param {number} ttlMs
 * @returns {number}
 */
export function clampCacheTtlMs(ttlMs) {
  const n = Number(ttlMs);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_CACHE_TTL_MS;
  return Math.min(MAX_CACHE_TTL_MS, Math.max(MIN_CACHE_TTL_MS, Math.floor(n)));
}

/**
 * @param {string} baseUrl e.g. https://ocdi.blitz.cloud (no trailing slash)
 * @param {string} path e.g. /api/v1/health
 */
export function joinBaseUrl(baseUrl, path) {
  const base = String(baseUrl || "").replace(/\/+$/, "");
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${base}${p}`;
}

/**
 * Satu GET — tidak melempar pada kegagalan jaringan (envelope konsisten).
 *
 * @returns {Promise<{ status: number, data: object|null, headers: Headers|object }>}
 */
export async function fetchMonitoringResource(
  url,
  { apiKey = null, signal = undefined, fetchImpl = globalThis.fetch } = {},
) {
  const headers = {};
  if (apiKey) headers["X-API-Key"] = apiKey;
  let res;
  try {
    res = await fetchImpl(url, { method: "GET", headers, signal });
  } catch (_) {
    return {
      status: 0,
      data: {
        error: {
          code: "E_SERVICE_UNAVAILABLE",
          message:
            "service tidak terjangkau (offline / cold start / di-restart)",
        },
      },
      headers: {},
    };
  }
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    data = null;
  }
  return { status: res.status, data, headers: res.headers };
}

function envelopeFromResponse(r) {
  if (r.status >= 200 && r.status < 300 && r.data && !r.data.error) {
    return { httpStatus: r.status, error: null, ok: true };
  }
  const err =
    r.data && r.data.error
      ? toError(r.data, r.status)
      : toError(
          {
            error: {
              code: r.status === 0 ? "E_SERVICE_UNAVAILABLE" : "E_HTTP_ERROR",
              message: r.status === 0 ? "service tidak terjangkau" : `HTTP ${r.status}`,
            },
          },
          r.status,
        );
  return { httpStatus: r.status, error: err, ok: false };
}

/**
 * B·01 + B·02: ambil & normalisasi semua sumber monitoring untuk satu site.
 *
 * @param {object} opts
 * @param {string} opts.baseUrl
 * @param {string} opts.siteId
 * @param {string} opts.apiKey
 * @param {number} [opts.historyLimit=50]
 * @param {AbortSignal} [opts.signal]
 * @param {typeof fetch} [opts.fetchImpl]
 * @returns {Promise<MonitoringSiteBundle>}
 */
export async function ingestMonitoringSite({
  baseUrl,
  siteId,
  apiKey,
  historyLimit = DEFAULT_HISTORY_LIMIT,
  signal,
  fetchImpl = globalThis.fetch,
}) {
  const sid = encodeURIComponent(siteId);
  const lim = Math.min(100, Math.max(1, Number(historyLimit) || DEFAULT_HISTORY_LIMIT));
  const common = { apiKey, signal, fetchImpl };

  const [rHealth, rLatest, rHistory, rPerf] = await Promise.all([
    fetchMonitoringResource(joinBaseUrl(baseUrl, "/api/v1/health"), {
      signal,
      fetchImpl,
    }),
    fetchMonitoringResource(
      joinBaseUrl(
        baseUrl,
        `/api/v1/forecast/latest?site_id=${sid}`,
      ),
      common,
    ),
    fetchMonitoringResource(
      joinBaseUrl(
        baseUrl,
        `/api/v1/forecast/history?site_id=${sid}&limit=${lim}`,
      ),
      common,
    ),
    fetchMonitoringResource(
      joinBaseUrl(
        baseUrl,
        `/api/v1/model/performance?site_id=${sid}`,
      ),
      common,
    ),
  ]);

  const healthEnv = envelopeFromResponse(rHealth);
  const latestEnv = envelopeFromResponse(rLatest);
  const historyEnv = envelopeFromResponse(rHistory);
  const perfEnv = envelopeFromResponse(rPerf);

  const forecast = latestEnv.ok ? toForecast(rLatest.data) : null;
  const history = historyEnv.ok ? toHistory(rHistory.data) : null;
  const performance = perfEnv.ok ? toPerformance(rPerf.data) : null;
  const health = healthEnv.ok ? toHealth(rHealth.data) : null;

  const generatedAt =
    forecast?.generatedAt ??
    (history?.items?.[0]?.generatedAt ?? null);
  const modelVersion =
    forecast?.modelVersion ??
    performance?.modelVersion ??
    (history?.items?.[0]?.modelVersion ?? null);

  return {
    siteId,
    fetchedAt: new Date().toISOString(),
    generatedAt,
    modelVersion,
    health: {
      ...healthEnv,
      health,
    },
    latest: {
      ...latestEnv,
      forecast,
    },
    history: {
      ...historyEnv,
      history,
    },
    performance: {
      ...perfEnv,
      performance,
    },
  };
}

/**
 * Cache per site (B·03): stale-while-revalidate opsional.
 */
export class MonitoringSiteCache {
  /**
   * @param {object} [opts]
   * @param {number} [opts.ttlMs]
   * @param {boolean} [opts.revalidateInBackground=true]
   */
  constructor({
    ttlMs = DEFAULT_CACHE_TTL_MS,
    revalidateInBackground = true,
  } = {}) {
    this.ttlMs = clampCacheTtlMs(ttlMs);
    this.revalidateInBackground = revalidateInBackground;
    /** @type {Map<string, { bundle: MonitoringSiteBundle, cachedAtMs: number, generatedAt: string|null }>} */
    this._entries = new Map();
    /** @type {Map<string, Promise<MonitoringSiteBundle>>} */
    this._inflight = new Map();
  }

  peek(siteId) {
    const e = this._entries.get(siteId);
    if (!e) return null;
    const ageMs = Date.now() - e.cachedAtMs;
    return {
      bundle: e.bundle,
      cachedAtMs: e.cachedAtMs,
      generatedAt: e.generatedAt,
      ageMs,
      fresh: ageMs < this.ttlMs,
    };
  }

  clear(siteId) {
    if (siteId == null) {
      this._entries.clear();
      this._inflight.clear();
      return;
    }
    this._entries.delete(siteId);
    this._inflight.delete(siteId);
  }

  /**
   * @param {string} siteId
   * @param {Parameters<typeof ingestMonitoringSite>[0]} ingestOpts — siteId boleh diabaikan (diisi dari argumen)
   * @returns {Promise<{ bundle: MonitoringSiteBundle, fromCache: boolean, stale: boolean }>}
   */
  async load(siteId, ingestOpts) {
    const peek = this.peek(siteId);
    if (peek && peek.fresh) {
      return { bundle: peek.bundle, fromCache: true, stale: false };
    }
    if (peek && this.revalidateInBackground) {
      this._revalidate(siteId, ingestOpts).catch(() => {});
      return { bundle: peek.bundle, fromCache: true, stale: true };
    }
    const bundle = await this._fetchOnce(siteId, ingestOpts);
    return { bundle, fromCache: false, stale: false };
  }

  async _fetchOnce(siteId, ingestOpts) {
    let pending = this._inflight.get(siteId);
    if (!pending) {
      pending = ingestMonitoringSite({ ...ingestOpts, siteId }).finally(() => {
        this._inflight.delete(siteId);
      });
      this._inflight.set(siteId, pending);
    }
    const bundle = await pending;
    this._entries.set(siteId, {
      bundle,
      cachedAtMs: Date.now(),
      generatedAt: bundle.generatedAt ?? null,
    });
    return bundle;
  }

  async _revalidate(siteId, ingestOpts) {
    await this._fetchOnce(siteId, ingestOpts);
  }
}
