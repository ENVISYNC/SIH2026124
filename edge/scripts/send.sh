#!/usr/bin/env bash
# Send one specific report to the server, on demand, during the demo.
#
# A thin, memorable wrapper over `uip_edge.inject` - the flags that matter are already
# chosen per recipe, so on stage you type one word instead of remembering that a pothole
# needs three buses and waterlogging needs a 1.5 h spread.
#
#   bash scripts/send.sh                        list the recipes
#   bash scripts/send.sh pothole                confirmed pothole, random spot on a route
#   bash scripts/send.sh pothole 18.5204 73.8567    ...at a spot you choose
#   bash scripts/send.sh pending                one bus only - stays OFF the map
#   bash scripts/send.sh accident               accident with ANPR plates, appears at once
#
# Reads UIP_SERVER_URL and UIP_INGEST_TOKEN; anything after the coordinates is passed
# straight through to `uip_edge.inject`, so `send.sh pothole --severity low` works.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(dirname "$(dirname "$SELF")")"
# --help reads the comment block out of this file, so $0 has to survive the cd
cd "$ROOT"

URL="${UIP_SERVER_URL:-http://localhost:8000}"
TOKEN="${UIP_INGEST_TOKEN:-dev-token-change-me}"

usage() {
    cat <<'MSG'
usage: bash scripts/send.sh <recipe> [lat lon] [extra inject flags]

  DEFECTS - these are the ones that must be corroborated
    pothole      3 distinct buses  -> CONFIRMED, appears on the map
    pending      1 bus             -> stays pending, deliberately invisible
    solo         1 bus x 4 passes over 8 h -> CONFIRMED by the repeated-pass fallback
    solo-fail    1 bus x 4 passes over 1 h -> still pending, shows the 6 h span guard
    water        2 buses inside the 3 h rain window -> CONFIRMED
    water-fail   2 buses spread over 8 h -> pending, shows why the window is short

  NO CONFIRMATION - one reading is enough
    jam          congestion, density 0.95   (red on the heatmap)
    flow         congestion, density 0.12   (green on the heatmap)
    accident     incident + ANPR plates, realtime lane
    rash         incident, rash driving

  UTILITIES
    reap         run the lifecycle pass now (merge fragments, expire stale rows)
    status       what the fleet has reported and what survived confirmation
    truth        the ground truth the buses never transmit

  With no lat/lon a random point on the real PMPML route network is used.
MSG
}

[ $# -ge 1 ] || { usage; exit 0; }
RECIPE="$1"; shift

# optional "lat lon" pair, else a random point on a route
WHERE=(--random)
if [ $# -ge 2 ] && [[ "$1" =~ ^-?[0-9.]+$ ]] && [[ "$2" =~ ^-?[0-9.]+$ ]]; then
    WHERE=(--lat "$1" --lon "$2"); shift 2
fi

inject() {  # inject <event_type> [flags...]
    local out
    # tee, because the deep link below needs the coordinates inject chose - especially
    # with --random, where nobody knows them until it has run
    out="$(uv run python -m uip_edge.inject "$@" "${WHERE[@]}" \
           --url "$URL" --token "$TOKEN" 2>&1 || true)"
    printf '%s\n' "$out"
    local coords
    coords="$(printf '%s' "$out" | sed -n 's/.* at \([0-9.-]*\), \([0-9.-]*\)$/\1 \2/p' | head -1)"
    if [ -n "$coords" ]; then
        # the map keeps its view in the URL hash, so this link opens straight on the spot
        printf '  map: %s/#17/%s\n' "$URL" "$(echo "$coords" | tr ' ' '/')"
    fi
}

case "$RECIPE" in
    pothole)    inject pothole   --buses 3 "$@" ;;
    pending)    inject pothole   --buses 1 "$@" ;;
    solo)       inject pothole   --buses 1 --passes 4 --spread-hours 8 "$@" ;;
    solo-fail)  inject pothole   --buses 1 --passes 4 --spread-hours 1 "$@" ;;
    water)      inject waterlogging --buses 2 "$@" ;;
    water-fail) inject waterlogging --buses 2 --spread-hours 8 "$@" ;;
    jam)        inject congestion --level 0.95 "$@" ;;
    flow)       inject congestion --level 0.12 "$@" ;;
    accident)   inject incident   --subtype accident "$@" ;;
    rash)       inject incident   --subtype rash_driving "$@" ;;

    reap)       curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
                     "$URL/api/v1/ops/reap"; echo ;;
    status)     curl -sf "$URL/api/v1/ops/stats"; echo ;;
    truth)      uv run python -m uip_edge.run --show-truth ;;

    -h|--help|help) usage ;;
    *) echo "unknown recipe: $RECIPE" >&2; echo >&2; usage >&2; exit 1 ;;
esac
