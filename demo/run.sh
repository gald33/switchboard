#!/usr/bin/env bash
# demo/run.sh — the coordination protocol, end to end, in one terminal.
#
# Two agents, two sessions, no shared memory between them:
#
#   alice posts a handoff to the blackboard, points to it with a short
#   message, and her session ends. beta starts fresh later — different
#   process, no memory of alice — reads the blackboard (not the message,
#   which would be long gone by then) and makes a decision that's
#   compatible with what alice already claimed.
#
# This dramatizes the worked example in docs/coordination-protocol.md.
# It is fully reproducible: it boots its own throwaway hub on a random
# port with a throwaway SQLite file, runs the two sessions against it,
# and tears the hub down on exit. Nothing you already have running is
# touched.
#
#   bash demo/run.sh
#
# Set DEMO_FAST=1 to skip the readability pauses (for smoke-testing the
# script itself rather than watching it).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# Always run against this checkout's source, not whatever `switchboard`
# happens to be on PATH — the point of a reproducible demo is that it
# shows the repo you're standing in, not a stale global install.
switchboard() { PYTHONPATH="$REPO_ROOT/src" python3 -m switchboard.cli "$@"; }

if ! PYTHONPATH="$REPO_ROOT/src" python3 -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
  echo "missing server deps — run: pip install -e '$REPO_ROOT[server]'" >&2
  exit 1
fi

PAUSE_SHORT=0.6
PAUSE_LONG=2.0
if [ "${DEMO_FAST:-0}" = "1" ]; then
  PAUSE_SHORT=0.02
  PAUSE_LONG=0.02
fi

WORKDIR="$(mktemp -d)"
PORT=$((20000 + RANDOM % 20000))
TOKEN="demo-token-$RANDOM"

cleanup() {
  [ -n "${HUB_PID:-}" ] && kill "$HUB_PID" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

dim()  { printf '\033[2m%s\033[0m' "$1"; }
bold() { printf '\033[1m%s\033[0m' "$1"; }

banner() {
  echo
  bold "── $1 ──"
  echo
  sleep "$PAUSE_LONG"
}

step() {
  local display="$1"; shift
  printf '\n'
  dim '$ '
  printf '%s\n' "$display"
  sleep "$PAUSE_SHORT"
  "$@"
  sleep "$PAUSE_LONG"
}

# --- infra: a throwaway hub, not part of the story --------------------------
# Backgrounded directly (not through the `switchboard` function above) so
# $! is the real server PID, not a wrapper subshell's — killing the wrong
# one would leak an orphaned uvicorn process on every run.
PYTHONPATH="$REPO_ROOT/src" python3 -m switchboard.cli --token "$TOKEN" \
  serve --host 127.0.0.1 --port "$PORT" \
  --db "$WORKDIR/hub.db" --log-level warning \
  > "$WORKDIR/hub.log" 2>&1 &
HUB_PID=$!

export SWITCHBOARD_URL="http://127.0.0.1:$PORT"
export SWITCHBOARD_TOKEN="$TOKEN"
export SWITCHBOARD_WORKSPACE="demo"

for _ in $(seq 1 100); do
  switchboard health >/dev/null 2>&1 && break
  sleep 0.1
done

# --- session 1: alice, local laptop, about to end her turn ------------------
export SWITCHBOARD_AGENT_ID=alice
banner "SESSION 1 — alice, local laptop"

# --ttl 5: presence defaults to a 2-minute expiry, which would still show
# alice as present when beta checks the roster a few seconds from now in
# this recording. Shortening it here compresses that into a demo-friendly
# few seconds without changing anything about how the protocol behaves.
step 'switchboard register --kind local -c build --ttl 5' \
  switchboard register --kind local -c build --ttl 5

step 'switchboard board list --prefix coord/' \
  switchboard board list --prefix coord/

step "switchboard board set coord/proposals/db-migration-order '{\"taken\":[\"0142\"],\"next_free\":\"0143\"}' --json-body" \
  switchboard board set coord/proposals/db-migration-order '{"taken":["0142"],"next_free":"0143"}' --json-body

step 'switchboard say build "posted migration order — see coord/proposals/db-migration-order"' \
  switchboard say build "posted migration order — see coord/proposals/db-migration-order"

printf '\n'
dim '  … alice'\''s turn ends here. session exits.'
printf '\n'
sleep "$PAUSE_LONG"

# --- time passes -------------------------------------------------------------
banner "2 HOURS LATER — new session, new machine"

# --- session 2: beta, cloud session, no memory of alice ---------------------
export SWITCHBOARD_AGENT_ID=beta
step 'switchboard register --kind cloud -c build' \
  switchboard register --kind cloud -c build

step 'switchboard agents' \
  switchboard agents

step 'switchboard board list --prefix coord/' \
  switchboard board list --prefix coord/

step 'switchboard board get coord/proposals/db-migration-order' \
  switchboard board get coord/proposals/db-migration-order

step "switchboard board set coord/status/beta '{\"decision\":\"took 0143 - compatible with the board\"}' --json-body" \
  switchboard board set coord/status/beta '{"decision":"took 0143 - compatible with the board"}' --json-body

step 'switchboard say build "took 0143 - compatible with the proposal on the board"' \
  switchboard say build "took 0143 - compatible with the proposal on the board"

# --- end on the shared status, not a feature list ----------------------------
banner "SHARED STATUS"
step 'switchboard board list --prefix coord/' \
  switchboard board list --prefix coord/
