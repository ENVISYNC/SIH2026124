#!/usr/bin/env bash
# ONE command to bring up the whole edge laptop: dependencies, a populated city, the
# lifecycle pass, and then the fleet driving live.
#
#   bash scripts/start.sh                              # server on this machine
#   bash scripts/start.sh --url http://192.168.1.5:8000
#   bash scripts/start.sh --hours 6                    # shorter backfill, ~45 s
#   bash scripts/start.sh --no-backfill                # straight to live
#   bash scripts/start.sh --no-live                    # populate, then hand back the prompt
#
# UIP_SERVER_URL and UIP_INGEST_TOKEN are honoured if already exported; --url and
# --token override them. Ctrl-C stops the live run and leaves the data on the server.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(dirname "$(dirname "$SELF")")"
# --help reads the comment block out of this file, so $0 has to survive the cd
cd "$ROOT"

URL="${UIP_SERVER_URL:-http://localhost:8000}"
TOKEN="${UIP_INGEST_TOKEN:-dev-token-change-me}"
HOURS=24; INCIDENTS=8; DROPOUT=0.04; SPEED=20
BACKFILL=1; LIVE=1; SEED=""

while [ $# -gt 0 ]; do
    case "$1" in
        --url)          URL="$2"; shift 2 ;;
        --token)        TOKEN="$2"; shift 2 ;;
        --hours)        HOURS="$2"; shift 2 ;;
        --incidents)    INCIDENTS="$2"; shift 2 ;;
        --dropout)      DROPOUT="$2"; shift 2 ;;
        --speed)        SPEED="$2"; shift 2 ;;
        --seed)         SEED="$2"; shift 2 ;;
        --no-backfill)  BACKFILL=0; shift ;;
        --no-live)      LIVE=0; shift ;;
        -h|--help)      sed -n '2,/^set /p' "$SELF" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1  (try --help)" >&2; exit 1 ;;
    esac
done

export UIP_SERVER_URL="$URL" UIP_INGEST_TOKEN="$TOKEN"
step() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

# `uv sync` from inside a workspace MEMBER installs only that member's dependencies and
# uninstalls the rest, so running it in central/ strips the edge half out of the shared
# venv - and doing that under a running server swaps loaded extension modules out from
# under it. Sync from the workspace root when there is one; in a bundle there is not, and
# ROOT is already the project root.
sync_root() {
    # Only the IMMEDIATE parent: that is the workspace root in the dev repo. Walking
    # further up would make a bundle sitting in dist/ inside the dev tree resolve to the
    # repo root, which is exactly the wrong answer for a bundle.
    local up
    up="$(dirname "$ROOT")"
    if [ -f "$up/pyproject.toml" ] && grep -q '^\[tool\.uv\.workspace\]' "$up/pyproject.toml"; then
        printf '%s' "$up"
    else
        printf '%s' "$ROOT"
    fi
}

step "dependencies"
(cd "$(sync_root)" && uv sync --quiet) && echo "uv sync ok"

# ---------------------------------------------------------------- reachability
# Worth failing here rather than three minutes into a backfill: on a two-laptop setup
# this is the step that catches a wrong IP, a firewall, or a server bound to loopback.
step "server at $URL"
printf 'waiting'
for _ in $(seq 30); do
    curl -sf -m 2 "$URL/healthz" >/dev/null 2>&1 && break
    printf '.'; sleep 1
done
echo
curl -sf -m 3 "$URL/healthz" || {
    cat >&2 <<MSG

cannot reach $URL

  * is the server laptop running  bash scripts/start.sh  ?
  * was it started with --host 0.0.0.0 ? the default binds loopback only
  * same network? conference wifi often isolates clients - use a phone hotspot
MSG
    exit 1
}
echo

# ---------------------------------------------------------------- populate

if [ "$BACKFILL" = 1 ]; then
    step "backfilling $HOURS h of fleet data (this takes a few minutes)"
    uv run python -m uip_edge.run backfill \
        --hours "$HOURS" --incidents "$INCIDENTS" --dropout "$DROPOUT" \
        ${SEED:+--seed "$SEED"}

    # merge cluster fragments and age out stale rows, so the map's lifecycle state is
    # current the moment it is first shown rather than up to 5 minutes stale
    step "running the lifecycle pass"
    curl -sf -X POST -H "Authorization: Bearer $TOKEN" "$URL/api/v1/ops/reap" && echo
fi

step "ready"
cat <<MSG
  map            $URL/
  send an event  bash scripts/send.sh            (lists the recipes)
  ground truth   uv run python -m uip_edge.run --show-truth
MSG

if [ "$LIVE" = 1 ]; then
    step "driving the fleet live (Ctrl-C to stop; the data stays on the server)"
    exec uv run python -m uip_edge.run live --speed "$SPEED"
fi
