#!/usr/bin/env bash
# Recapture site/design/img/viewer-mobile.jpg.
#
# Against a THROWAWAY hub, on purpose. Pointing the viewer at a real room
# means minting an invite, and `switchboard invite` seals a value onto the
# board — a write to a live room. A screenshot is not worth that, so this
# stages its own room the way demo/run.sh stages its own hub.
#
#   bash site/design/capture-viewer.sh
#
# Needs Playwright and a local Chrome. The click matters: the invite arrives
# as a dialog, and headless `--screenshot` alone photographs the form rather
# than the room. See img/README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/site/design/img/viewer-mobile.png}"
WORK="$(mktemp -d)"
PORT=$((21000 + RANDOM % 15000))
WEBPORT=$((22000 + RANDOM % 15000))
export SWITCHBOARD_TOKEN="shot-$RANDOM"

sb() { PYTHONPATH="$ROOT/src" python3 -m switchboard.cli "$@"; }

PYTHONPATH="$ROOT/src" python3 -m switchboard.cli serve \
  --host 127.0.0.1 --port "$PORT" --db "$WORK/s.db" \
  --cors-origin "http://127.0.0.1:$WEBPORT" >"$WORK/hub.log" 2>&1 &
HUB=$!
python3 -m http.server "$WEBPORT" -b 127.0.0.1 \
  -d "$ROOT/extras/viewer/switchboard_viewer/web" >/dev/null 2>&1 &
WEB=$!
trap 'kill $HUB $WEB 2>/dev/null || true; rm -rf "$WORK"' EXIT

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done

export SWITCHBOARD_URL="http://127.0.0.1:$PORT"
eval "$(sb keygen --env)"
export SWITCHBOARD_WORKSPACE="${SWITCHBOARD_WORKSPACE:-demo-room}"

SWITCHBOARD_AGENT_ID=alice sb announce --name alice --kind local -c build --task "migrations 0142" >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb announce --name beta  --kind cloud -c build --task "api handlers"   >/dev/null
SWITCHBOARD_AGENT_ID=ci-7  sb announce --name ci-7  --kind ci    -c build --task "test matrix"    >/dev/null
SWITCHBOARD_AGENT_ID=alice sb claim db/migrations -m "adding 0142"      >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb claim api/handlers  -m "routing rewrite"  >/dev/null
SWITCHBOARD_AGENT_ID=alice sb say build "migrations are mine for ~15m"                    >/dev/null
SWITCHBOARD_AGENT_ID=beta  sb say build "taking api/handlers, no overlap with 0142"       >/dev/null
SWITCHBOARD_AGENT_ID=ci-7  sb say build "matrix green on 3.11 and 3.12"                   >/dev/null
SWITCHBOARD_AGENT_ID=alice sb board set coord/proposals/db-migration-order \
  '{"taken":["0142"],"next_free":"0143"}' --json-body >/dev/null

LINK="$(sb invite --link "http://127.0.0.1:$WEBPORT/index.html" --no-input \
        | tr -d '\n' | grep -o 'http[^ ]*')"

OUT="$OUT" python3 - "$LINK" <<'PY'
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    pg.goto(sys.argv[1], wait_until="networkidle")
    pg.get_by_role("button", name="Add room").first.click()   # the dialog, not the room, without this
    pg.wait_for_timeout(3500)
    pg.screenshot(path=os.environ["OUT"])
    b.close()
PY

echo "wrote $OUT — downsample before use:"
echo "  sips -Z 760 '$OUT' && sips -s format jpeg -s formatOptions 72 '$OUT' --out '${OUT%.png}.jpg'"
