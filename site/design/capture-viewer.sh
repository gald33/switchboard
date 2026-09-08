set -euo pipefail
ROOT="$1"; OUT="$2"
export SWITCHBOARD_TOKEN="shot-$RANDOM"
PORT=$((21000 + RANDOM % 15000)); WEBPORT=$((22000 + RANDOM % 15000))
WORK="$(mktemp -d)"
sb() { PYTHONPATH="$ROOT/src" python3 -m switchboard.cli "$@"; }

PYTHONPATH="$ROOT/src" python3 -m switchboard.cli serve --host 127.0.0.1 --port "$PORT" \
  --db "$WORK/s.db" --cors-origin "http://127.0.0.1:$WEBPORT" >"$WORK/hub.log" 2>&1 &
HUB=$!
python3 -m http.server "$WEBPORT" -d "$ROOT/extras/viewer/switchboard_viewer/web" -b 127.0.0.1 >/dev/null 2>&1 &
WEB=$!
trap 'kill $HUB $WEB 2>/dev/null || true; rm -rf "$WORK"' EXIT
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 0.25; done

export SWITCHBOARD_URL="http://127.0.0.1:$PORT"
eval "$(sb keygen --env 2>/dev/null || true)"
export SWITCHBOARD_WORKSPACE="${SWITCHBOARD_WORKSPACE:-demo-room}"

SWITCHBOARD_AGENT_ID=alice sb announce --name alice --kind local -c build --task "migrations 0142" >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb announce --name beta  --kind cloud -c build --task "api handlers" >/dev/null
SWITCHBOARD_AGENT_ID=ci-7  sb announce --name ci-7  --kind ci    -c build --task "test matrix" >/dev/null
SWITCHBOARD_AGENT_ID=alice sb claim db/migrations -m "adding 0142" >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb claim api/handlers  -m "routing rewrite" >/dev/null
SWITCHBOARD_AGENT_ID=alice sb say build "migrations are mine for ~15m" >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb say build "taking api/handlers, no overlap with 0142" >/dev/null
SWITCHBOARD_AGENT_ID=ci-7  sb say build "matrix green on 3.11 and 3.12" >/dev/null
SWITCHBOARD_AGENT_ID=alice sb board set coord/proposals/db-migration-order '{"taken":["0142"],"next_free":"0143"}' --json-body >/dev/null

LINK="$(sb invite --link "http://127.0.0.1:$WEBPORT/index.html" --no-input 2>/dev/null | tr -d '\n' | grep -o 'http[^ ]*')"
echo "link: ${LINK:0:60}…"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --hide-scrollbars \
  --window-size=390,844 --force-device-scale-factor=2 --virtual-time-budget=6000 \
  --screenshot="$OUT" "$LINK" 2>/dev/null || true
ls -la "$OUT"
