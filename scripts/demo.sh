#!/usr/bin/env bash
# Bring the WHOLE demo up on one laptop, for practice runs and for the fallback when the
# second machine or the network is not cooperating (demo.md section 11).
#
#   bash scripts/demo.sh              # server + 24 h backfill + live fleet
#   bash scripts/demo.sh --hours 6    # quicker, for a rehearsal
#   bash scripts/demo.sh --reset      # truncate first, for a clean run
#
# On the real two-laptop setup do NOT use this - run central/scripts/start.sh on the
# server and edge/scripts/start.sh on the edge laptop, so the two halves stay genuinely
# separate machines talking over the LAN. That separation is part of what is being shown.
#
# Ctrl-C stops both halves.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(dirname "$(dirname "$SELF")")"
# --help reads the comment block out of this file, so $0 has to survive the cd
cd "$ROOT"

SERVER_ARGS=(); EDGE_ARGS=(); PORT=8000
while [ $# -gt 0 ]; do
    case "$1" in
        --reset)  SERVER_ARGS+=(--reset); shift ;;
        --port)   PORT="$2"; SERVER_ARGS+=(--port "$2");
                  EDGE_ARGS+=(--url "http://localhost:$2"); shift 2 ;;
        --hours|--speed|--incidents|--dropout|--seed) EDGE_ARGS+=("$1" "$2"); shift 2 ;;
        --no-backfill|--no-live) EDGE_ARGS+=("$1"); shift ;;
        -h|--help) sed -n '2,/^set /p' "$SELF" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1  (try --help)" >&2; exit 1 ;;
    esac
done

LOG="$(mktemp -t uip-server-XXXX.log)"
echo "server log -> $LOG"

bash central/scripts/start.sh "${SERVER_ARGS[@]}" >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'echo; echo "stopping"; kill $SERVER_PID 2>/dev/null || true' INT TERM EXIT

printf 'starting the server'
for _ in $(seq 90); do
    curl -sf -m 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break
    kill -0 $SERVER_PID 2>/dev/null || { echo; echo "server failed - see $LOG" >&2; tail -20 "$LOG" >&2; exit 1; }
    printf '.'; sleep 1
done
echo " up"

# The server's own banner went into $LOG, but in the common variant of this setup the
# dashboard is on a SECOND laptop and its browser needs this address - so print it here
# too rather than making anyone go and read the log for it.
IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -n "${IP:-}" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${IP:-}" ] || IP="<this-laptop-ip>"
cat <<BANNER

  ┌─────────────────────────────────────────────────────────────┐
   map        http://$IP:$PORT/          <- open this on the dashboard laptop
   local      http://localhost:$PORT/
   API docs   http://$IP:$PORT/docs
  └─────────────────────────────────────────────────────────────┘

BANNER

bash edge/scripts/start.sh "${EDGE_ARGS[@]}"
