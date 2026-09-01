# wake-on-message — park on the hub until something arrives, then exit so the
# agent runner wakes the session.
#
# Installed by `switchboard init` as .switchboard/wake-on-message.sh, with the
# hub URL, the workspace and the `sb` bootstrap prepended the same way the
# lifecycle hooks get them. This file is the body; it is not standalone, and
# `sb` is a shell function defined above it.
#
# The problem it solves is the turn boundary. A coding session only reaches for
# its inbox when it is its turn, so a message that lands after the turn ends is
# delivered to nobody until a human types something. A runner that re-invokes a
# session when a background process *exits* — Claude Code does — makes process
# death an available wake signal. This turns "a message arrived" into "a
# process exited", which is as close to a webhook as an agent session gets:
#
#   run it with run_in_background, and the session is woken, with the message
#   on stdout, within a second of it being posted.
#
# Three things it is careful about, all of which are silent when got wrong:
#
#   It peeks. This process shares an agent id with the session that started it
#   (both derive it from the harness session id), which means they share a read
#   cursor. A draining listener would consume the very message it woke the
#   session to read, and the session would find an empty inbox and go back to
#   sleep. So it never advances the cursor: it looks, prints what it saw, and
#   exits. The woken session does the real drain.
#
#   It heartbeats. A listener that dies looks exactly like a room where nothing
#   is happening — no error, no message, just quiet. Each pass writes a
#   blackboard entry with a TTL of a few passes' worth, so a live listener is a
#   key whose revision advances and a dead one is a key that expired. Nothing
#   has to notice the death for it to become visible in `board list`.
#
#   It refuses to start sealed-blind. `switchboard` does not read the workspace
#   key out of .claude/settings.local.json, because a plain shell is not the
#   MCP subprocess that file's env reaches. In an encrypted room a listener
#   without the key watches a room it cannot address and finds nothing, ever.
#
# One wake, not a subscription: it exits on the first message, so a session
# that is still waiting has to arm it again before its next turn ends.
#
# Usage:
#   sh .switchboard/wake-on-message.sh              # this agent's own inbox
#   sh .switchboard/wake-on-message.sh -c deploys   # ...or named channels
#
# Environment:
#   SWITCHBOARD_LISTEN_TTL       heartbeat TTL, seconds (default 90)
#   SWITCHBOARD_LISTEN_MAX_FAILS consecutive hub errors tolerated (default 5)
#   SWITCHBOARD_LISTEN_PASSES    stop after N quiet passes (default 0 = forever)

set -u

TTL="${SWITCHBOARD_LISTEN_TTL:-90}"
MAX_FAILS="${SWITCHBOARD_LISTEN_MAX_FAILS:-5}"
MAX_PASSES="${SWITCHBOARD_LISTEN_PASSES:-0}"

# `whoami` answers from local configuration and never dials the hub, so it
# would happily describe an identity on a hub that is down. Ask `health` too: a
# listener that cannot reach the hub should say so now, in a terminal someone
# is looking at, rather than in a wake five failed passes from now.
if ! sb -q health >/dev/null 2>&1; then
  echo "wake-on-message: hub unreachable — check SWITCHBOARD_URL and SWITCHBOARD_TOKEN" >&2
  exit 1
fi

WHOAMI="$(sb --json whoami 2>/dev/null)" || {
  echo "wake-on-message: cannot resolve this agent's identity" >&2
  exit 1
}
eval "$(
  python3 - "$WHOAMI" <<'PY'
import json, shlex, sys
d = json.loads(sys.argv[1])
for k in ("agent_id", "workspace", "encrypted"):
    print(f"WHO_{k.upper()}={shlex.quote(str(d.get(k)))}")
PY
)"

if [ "$WHO_ENCRYPTED" != "True" ] && [ -f .claude/settings.local.json ] \
   && grep -q SWITCHBOARD_KEY .claude/settings.local.json 2>/dev/null; then
  echo "wake-on-message: this repo has a workspace key but this shell does not." >&2
  echo "  The listener would read a different room than the session it serves" >&2
  echo "  and never see a thing. Export it first:" >&2
  echo "    export \$(switchboard whoami --env | xargs)" >&2
  exit 1
fi

HEARTBEAT="listener/$WHO_AGENT_ID"
PASS=0
FAILS=0

cleanup() {
  # Best effort, and only best effort: the TTL is what actually makes a dead
  # listener visible. This just makes a *deliberate* stop visible sooner.
  sb -q board delete "$HEARTBEAT" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "wake-on-message: parked as $WHO_AGENT_ID on $WHO_WORKSPACE (heartbeat $HEARTBEAT)" >&2

while :; do
  PASS=$((PASS + 1))

  sb -q board set "$HEARTBEAT" \
    "{\"pass\": $PASS, \"pid\": $$, \"waiting_on\": \"inbox\"}" \
    --json-body --ttl "$TTL" >/dev/null 2>&1 \
    || echo "wake-on-message: heartbeat write failed (pass $PASS)" >&2

  if MESSAGES="$(sb --json inbox --peek --wait 25 "$@" 2>/dev/null)"; then
    FAILS=0
    if printf '%s' "$MESSAGES" | python3 -c \
         'import json,sys; sys.exit(0 if json.load(sys.stdin)["messages"] else 1)'; then
      # The payload goes to stdout because the wake carries it: the session
      # comes back already holding the event instead of having to go ask.
      echo "$MESSAGES"
      echo "wake-on-message: message arrived on pass $PASS — waking the session" >&2
      exit 0
    fi
  else
    FAILS=$((FAILS + 1))
    echo "wake-on-message: hub error ($FAILS/$MAX_FAILS)" >&2
    if [ "$FAILS" -ge "$MAX_FAILS" ]; then
      # Exiting non-zero is still a wake, and that is the point: the session
      # finds out the listener is gone rather than mistaking a broken hub for
      # a quiet room.
      echo "wake-on-message: giving up after $FAILS consecutive failures" >&2
      exit 1
    fi
    sleep $((FAILS * 2))
  fi

  if [ "$MAX_PASSES" -gt 0 ] && [ "$PASS" -ge "$MAX_PASSES" ]; then
    echo "wake-on-message: stopping after $PASS quiet pass(es), as configured" >&2
    exit 2
  fi
done
