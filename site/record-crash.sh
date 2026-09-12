#!/usr/bin/env bash
# site/record-crash.sh — capture an agent dying with a job still claimed.
#
# The page claims a lease releases itself when the agent holding it stops.
# This records that happening: a throwaway hub, one worker killed with -9 in
# the middle of a task (so nothing runs on the way out and nothing is
# released), the claim sitting there, and a second worker taking the same task
# once the lease lapses.
#
#   bash site/record-crash.sh > site/clips/crash.txt
#
# Lines beginning `#` are this script's, not the run's. Everything else is
# output. Takes about ninety seconds: the lease TTL in the example is 60s.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
PORT=$((20000 + RANDOM % 20000))
TOKEN="crash-$RANDOM"

PYTHONPATH="$REPO/src" python3 -m switchboard.cli --token "$TOKEN" serve \
  --host 127.0.0.1 --port "$PORT" --db "$WORKDIR/hub.db" --log-level warning \
  > "$WORKDIR/hub.log" 2>&1 &
HUB_PID=$!
cleanup() { kill "$HUB_PID" 2>/dev/null || true; rm -rf "$WORKDIR"; }
trap cleanup EXIT

export SWITCHBOARD_URL="http://127.0.0.1:$PORT"
export SWITCHBOARD_TOKEN="$TOKEN"
export SWITCHBOARD_WORKSPACE=demo

for _ in $(seq 1 100); do
  PYTHONPATH="$REPO/src" python3 -m switchboard.cli health >/dev/null 2>&1 && break
  sleep 0.1
done

swb() { PYTHONPATH="$REPO/src" python3 -m switchboard.cli "$@"; }
stamp() { while IFS= read -r line; do printf '%s %s\n' "$(date -u +%H:%M:%S)" "$line"; done; }
note() { printf '# %s\n' "$1"; }

PYTHONPATH="$REPO/src" SWITCHBOARD_AGENT_ID=alice \
  python3 -u "$REPO/examples/coordinated_worker.py" alice 2>&1 | stamp &
ALICE=$!
sleep 1.5

note "alice is working. kill -9, so nothing runs on her way out."
# `wait` after the kill reaps her quietly: without it the shell announces the
# death itself, and that line is bash's, not the run's.
pkill -9 -f "coordinated_worker.py alice" 2>/dev/null || true
kill -9 "$ALICE" 2>/dev/null || true
wait "$ALICE" 2>/dev/null || true
sleep 0.5
swb claims | stamp

note "the lease in this example is 60s. waiting it out."
sleep 62
swb claims | stamp

note "bob starts, and asks for the same list."
PYTHONPATH="$REPO/src" SWITCHBOARD_AGENT_ID=bob \
  python3 -u "$REPO/examples/coordinated_worker.py" bob 2>&1 | stamp
