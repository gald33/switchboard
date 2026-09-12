#!/usr/bin/env bash
# site/record-collision.sh — capture three workers dividing one work list.
#
# The landing page shows four lines of this run. They have to come from a real
# run for the same reason the terminal recording does (PRINCIPLES.md 4), so
# this script boots a throwaway hub on a random port, runs three copies of
# examples/coordinated_worker.py against it, stamps each line with the second
# it appeared, and tears the hub down. Nothing you have running is touched.
#
#   bash site/record-collision.sh > site/clips/collision.txt
#
# Re-run it whenever the example's output changes. The page quotes consecutive
# lines from the capture, so a stale file is a page that quotes a run nobody
# can reproduce.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
PORT=$((20000 + RANDOM % 20000))
TOKEN="collision-$RANDOM"

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

stamp() { while IFS= read -r line; do printf '%s %s\n' "$(date -u +%H:%M:%S)" "$line"; done; }

pids=()
for name in alice bob carol; do
  PYTHONPATH="$REPO/src" SWITCHBOARD_AGENT_ID="$name" \
    python3 -u "$REPO/examples/coordinated_worker.py" "$name" 2>&1 | stamp &
  pids+=($!)
done
# Only the workers — `wait` with no arguments would also wait on the hub,
# which never exits on its own.
for pid in "${pids[@]}"; do wait "$pid"; done
