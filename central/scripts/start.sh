#!/usr/bin/env bash
# ONE command to bring up the whole server laptop: database, dependencies, API and map.
#
#   bash scripts/start.sh                # normal start
#   bash scripts/start.sh --reset        # truncate the tables first, for a clean run
#   bash scripts/start.sh --smoke        # run the 34-check smoke test once it is up
#   bash scripts/start.sh --port 9000
#
# Safe to re-run: every step checks the current state before acting, so an existing
# container is started rather than recreated and existing data is left alone.
# Ctrl-C stops the server; the database container keeps running (see --stop-db).
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(dirname "$(dirname "$SELF")")"
# --help reads the comment block out of this file, so $0 has to survive the cd
cd "$ROOT"

PORT=8000; RESET=0; SMOKE=0; STOPDB=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --reset)    RESET=1; shift ;;
        --smoke)    SMOKE=1; shift ;;
        --stop-db)  STOPDB=1; shift ;;
        -h|--help)  sed -n '2,/^set /p' "$SELF" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1  (try --help)" >&2; exit 1 ;;
    esac
done

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }

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

# ---------------------------------------------------------------- docker

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
# most installs need root for the daemon socket; find out once rather than sprinkling
# sudo through the script (and rather than demanding it when it is not needed)
if docker info >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo docker"; fi

if [ "$STOPDB" = 1 ]; then
    step "stopping the database container"
    $DOCKER stop uip-db && echo "stopped. The named volume keeps the data."
    exit 0
fi

step "database"
# newer docker prints a blank line on stdout even when the container does not exist
state="$($DOCKER inspect -f '{{.State.Status}}' uip-db 2>/dev/null | tr -d '[:space:]' || true)"
[ -n "$state" ] || state=missing
case "$state" in
    running) echo "uip-db already running" ;;
    missing) echo "creating uip-db..."; $DOCKER run -d --name uip-db \
                 -e POSTGRES_USER=uip -e POSTGRES_PASSWORD=uip -e POSTGRES_DB=uip \
                 -p 5432:5432 -v uip-pgdata:/var/lib/postgresql/data \
                 postgis/postgis:16-3.4 >/dev/null ;;
    *)       echo "starting the existing uip-db (the volume still has the data)..."
             $DOCKER start uip-db >/dev/null ;;
esac

printf 'waiting for postgres'
for _ in $(seq 60); do
    $DOCKER exec uip-db pg_isready -U uip -d uip >/dev/null 2>&1 && break
    printf '.'; sleep 1
done
echo
$DOCKER exec uip-db pg_isready -U uip -d uip >/dev/null 2>&1 \
    || { echo "postgres did not come up - check: $DOCKER logs uip-db" >&2; exit 1; }
echo "postgres ready on localhost:5432"

# ---------------------------------------------------------------- python deps

step "dependencies"
(cd "$(sync_root)" && uv sync --quiet) && echo "uv sync ok"

if [ "$RESET" = 1 ]; then
    step "resetting the tables"
    uv run python scripts/reset_db.py
fi

# ---------------------------------------------------------------- the server

# The schema is applied on startup by db.apply_schema(), so there is no migration step.
step "starting the API and map on 0.0.0.0:$PORT"
# --host 0.0.0.0 is NOT optional on a two-laptop setup: the default binds loopback only
# and the edge laptop cannot reach it.
uv run uvicorn uip_central.main:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!
trap 'echo; echo "stopping the server (the database keeps running)"; kill $SERVER_PID 2>/dev/null || true' INT TERM

printf 'waiting for the server'
for _ in $(seq 40); do
    curl -sf -m 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break
    kill -0 $SERVER_PID 2>/dev/null || { echo; echo "the server exited during startup" >&2; exit 1; }
    printf '.'; sleep 0.5
done
echo

IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -n "${IP:-}" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${IP:-}" ] || IP="<this-laptop-ip>"

cat <<BANNER

  ┌─────────────────────────────────────────────────────────────┐
   map        http://$IP:$PORT/
   API docs   http://$IP:$PORT/docs
   health     http://$IP:$PORT/healthz
  └─────────────────────────────────────────────────────────────┘

  On the EDGE laptop:
      export UIP_SERVER_URL=http://$IP:$PORT
      bash scripts/start.sh

BANNER

if [ "$SMOKE" = 1 ]; then
    step "smoke test"
    # NOTE: this posts its fixtures ~39 km outside Pune so it cannot disturb the fleet's
    # data, but it does leave rows behind. Run it BEFORE the backfill, not during a demo.
    uv run python scripts/smoke_test.py "http://localhost:$PORT" || warn "smoke test failed"
fi

wait $SERVER_PID
