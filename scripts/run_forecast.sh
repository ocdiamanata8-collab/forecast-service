#!/usr/bin/env bash
# Trigger scheduled forecast job for each configured site.
#
# Required environment:
#   FORECAST_API_URL   Service origin, e.g. https://forecast.example.com
#   FORECAST_API_KEY   Consumer API key; load from a secret manager
# Optional environment:
#   SITE_IDS           Comma-separated site IDs (default: PLTS_KUPANG)
#   FORECAST_HORIZON   Daily horizon 1..30 (default: 24)
#
# Example cron (set secrets/environment in the cron service, not this file):
#   0 * * * * /srv/forecast-service/scripts/run_forecast.sh
#
# This script calls the internal job endpoint, not POST /api/v1/forecast:
# the latter is on-demand and deliberately does not store results.
set -Eeuo pipefail

fail() {
  printf 'run_forecast: ERROR: %s\n' "$*" >&2
  exit 1
}

: "${FORECAST_API_URL:?Set FORECAST_API_URL to service origin}"
: "${FORECAST_API_KEY:?Set FORECAST_API_KEY from secret manager}"

# Trim trailing slash; prevent accidental double slash in endpoint path.
FORECAST_API_URL="${FORECAST_API_URL%/}"
[[ "$FORECAST_API_URL" =~ ^https?://[^[:space:]]+$ ]] \
  || fail 'FORECAST_API_URL must be an http(s) origin/URL without whitespace'

# token_urlsafe keys use this alphabet. Reject quotes/newlines before passing
# header through curl config stdin; key never appears in curl process arguments.
[[ "$FORECAST_API_KEY" =~ ^[A-Za-z0-9_-]{16,}$ ]] \
  || fail 'FORECAST_API_KEY has invalid format'

SITE_IDS="${SITE_IDS:-PLTS_KUPANG}"
FORECAST_HORIZON="${FORECAST_HORIZON:-24}"
[[ "$FORECAST_HORIZON" =~ ^[0-9]+$ ]] \
  || fail 'FORECAST_HORIZON must be integer 1..30'
(( FORECAST_HORIZON >= 1 && FORECAST_HORIZON <= 30 )) \
  || fail 'FORECAST_HORIZON must be integer 1..30'

command -v curl >/dev/null 2>&1 || fail 'curl is required'

# Split comma-separated site list; IDs must match API site_id pattern.
IFS=',' read -r -a sites <<< "$SITE_IDS"
((${#sites[@]} > 0)) || fail 'SITE_IDS is empty'

for site_id in "${sites[@]}"; do
  # Trim surrounding whitespace from each entry.
  site_id="$(printf '%s' "$site_id" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [[ "$site_id" =~ ^[A-Z]{2,4}[_A-Z0-9]{2,12}$ ]] \
    || fail "invalid site_id: $site_id"

  printf 'Starting forecast job: site=%s horizon=%s\n' "$site_id" "$FORECAST_HORIZON"
  payload="$(printf '{"site_id":"%s","horizon":%s}' "$site_id" "$FORECAST_HORIZON")"

  # curl reads secret header through stdin config, keeping key out of argv.
  # --fail-with-body makes HTTP 4xx/5xx a nonzero exit while preserving body.
  curl --config - \
    --silent --show-error --fail-with-body \
    --connect-timeout 10 --max-time 180 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data "$payload" \
    "${FORECAST_API_URL}/internal/v1/jobs/forecast" <<CURL_CONFIG
header = "X-API-Key: ${FORECAST_API_KEY}"
CURL_CONFIG
  printf '\n'
done

printf 'All forecast jobs completed.\n'
