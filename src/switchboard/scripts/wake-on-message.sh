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
# The deadline is the other half of that. Parking with no end is a promise to
# be reachable that nothing keeps: the session is idle, and if no message ever
# comes, nothing brings it back. `--until` sets when to give up and return
# empty, so arming is a bounded wait rather than an open one — and the exit
# says which happened, so the agent knows whether it was woken or simply
# reached the time it named.
#
# `--until forecast:p50` (or `:p95`) takes that time from the agent's own
# adaptive-timing model rather than a guess. The quantile is the posture: p50
# comes back early and often, p95 stays away longer and is disturbed less. On
# a machine with history that is a measurement; in a fresh container it is the
# bootstrap prior, deliberately wide — `source` in the exit line says which,
# and a deadline built on a prior should be a shorter one.
#
# Usage:
#   sh .switchboard/wake-on-message.sh              # this agent's own inbox
#   sh .switchboard/wake-on-message.sh -c deploys   # ...or named channels
#   sh .switchboard/wake-on-message.sh --until forecast:p50 --effort medium
#   sh .switchboard/wake-on-message.sh --until 2026-09-01T06:30:00Z
#
# Exit codes, which are how a woken agent tells the cases apart:
#   0  a message arrived (it is on stdout, peeked, still unread)
#   1  misconfigured or the hub stayed unreachable — nothing was watched
#   2  the deadline passed, or the pass limit did, with nothing to report
#
# Options:
#   --until VALUE                deadline: an ISO-8601 time, +SECONDS, or
#                                forecast:p50 / forecast:p95
#   --effort low|medium|high     passed to the forecast, with...
#   --execution-class LABEL      ...the kind of work ahead
#
# Environment:
#   SWITCHBOARD_LISTEN_TTL       heartbeat TTL, seconds (default 90)
#   SWITCHBOARD_LISTEN_MAX_FAILS consecutive hub errors tolerated (default 5)
#   SWITCHBOARD_LISTEN_PASSES    stop after N quiet passes (default 0 = forever)
#   SWITCHBOARD_LISTEN_UNTIL     same as --until

set -u

TTL="${SWITCHBOARD_LISTEN_TTL:-90}"
MAX_FAILS="${SWITCHBOARD_LISTEN_MAX_FAILS:-5}"
MAX_PASSES="${SWITCHBOARD_LISTEN_PASSES:-0}"
UNTIL="${SWITCHBOARD_LISTEN_UNTIL:-}"
EFFORT=""
EXECUTION_CLASS=""

# Our own flags are consumed here and must come first; the first argument we
# do not recognise ends the loop and everything from there is handed to
# `inbox` untouched, which is what makes `-c <channel>` work without this
# script having to know about channels. Stopping at the first unknown argument
# rather than sieving the whole list keeps "$@" intact, so a channel name with
# a space in it survives.
while [ $# -gt 0 ]; do
  case "$1" in
    --until) UNTIL="${2:-}"; shift 2 ;;
    --until=*) UNTIL="${1#--until=}"; shift ;;
    --effort) EFFORT="${2:-}"; shift 2 ;;
    --effort=*) EFFORT="${1#--effort=}"; shift ;;
    --execution-class) EXECUTION_CLASS="${2:-}"; shift 2 ;;
    --execution-class=*) EXECUTION_CLASS="${1#--execution-class=}"; shift ;;
    *) break ;;
  esac
done

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

# Resolved once, not per pass: a forecast is "when will I next look, counting
# from now", so re-asking each time would push the deadline away every 25
# seconds and the listener would never reach it.
DEADLINE=""
DEADLINE_SOURCE=""
if [ -n "$UNTIL" ]; then
  case "$UNTIL" in
    forecast:*)
      QUANTILE="${UNTIL#forecast:}"
      FORECAST="$(sb --json timing \
        ${EFFORT:+--effort "$EFFORT"} \
        ${EXECUTION_CLASS:+--execution-class "$EXECUTION_CLASS"} 2>/dev/null)" || FORECAST=""
      if [ -z "$FORECAST" ]; then
        echo "wake-on-message: could not read the local timing model for --until $UNTIL" >&2
        exit 1
      fi
      # Captured, checked, *then* evaluated. `eval "$(...)" || exit` cannot
      # work here: eval of an empty string succeeds, so a rejected quantile
      # printed its complaint and the listener parked with no deadline at all
      # — silently unbounded, which is the one outcome this flag exists to
      # prevent.
      FORECAST_VARS="$(
        printf '%s' "$FORECAST" | python3 -c '
import json, shlex, sys
q = sys.argv[1]
if q not in ("p50", "p95"):
    sys.exit("wake-on-message: --until forecast:%s — only p50 and p95 are published" % q)
f = json.load(sys.stdin).get("forecast") or {}
if not f.get(q):
    sys.exit("wake-on-message: the timing model published no %s" % q)
# Seconds from now rather than the absolute time: the forecast was computed
# against the model'"'"'s clock, and a listener that trusted the timestamp would
# inherit any skew between it and ours.
print("DEADLINE=%d" % (int(__import__("time").time()) + int(f["%s_in_seconds" % q])))
print("DEADLINE_SOURCE=" + shlex.quote("%s %s, %s sample(s)" % (
    q, f.get("source", "?"), f.get("samples", "?"))))
' "$QUANTILE"
      )" || exit 1
      eval "$FORECAST_VARS"
      ;;
    +*)
      DEADLINE=$(( $(date +%s) + ${UNTIL#+} ))
      DEADLINE_SOURCE="+${UNTIL#+}s"
      ;;
    *)
      DEADLINE="$(python3 -c '
import datetime, sys
raw = sys.argv[1].replace("Z", "+00:00")
try:
    t = datetime.datetime.fromisoformat(raw)
except ValueError:
    sys.exit("wake-on-message: --until %s is not an ISO-8601 time, +SECONDS, or forecast:p50/p95" % sys.argv[1])
if t.tzinfo is None:
    t = t.astimezone()
print(int(t.timestamp()))
' "$UNTIL")" || exit 1
      DEADLINE_SOURCE="given"
      ;;
  esac
fi

if [ -n "$DEADLINE" ]; then
  UNTIL_ISO="$(python3 -c 'import datetime,sys;print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).isoformat())' "$DEADLINE")"
  if [ "$DEADLINE" -le "$(date +%s)" ]; then
    echo "wake-on-message: deadline $UNTIL_ISO ($DEADLINE_SOURCE) is already past" >&2
    exit 2
  fi
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

echo "wake-on-message: parked as $WHO_AGENT_ID on $WHO_WORKSPACE (heartbeat $HEARTBEAT)${DEADLINE:+ until $UNTIL_ISO [$DEADLINE_SOURCE]}" >&2

while :; do
  PASS=$((PASS + 1))

  # Never poll past the deadline. Without this the last long-poll could
  # overshoot it by up to 25 seconds, which is the whole margin on a short
  # DND and turns a time the agent named into one it merely approximated.
  WAIT=25
  if [ -n "$DEADLINE" ]; then
    LEFT=$((DEADLINE - $(date +%s)))
    [ "$LEFT" -lt "$WAIT" ] && WAIT="$LEFT"
    if [ "$WAIT" -le 0 ]; then
      echo "wake-on-message: reached $UNTIL_ISO [$DEADLINE_SOURCE] with nothing to report" >&2
      exit 2
    fi
  fi

  # Announce presence as well as the board key. A parked agent is the most
  # reachable an agent ever is, and without this the roster says it does not
  # exist: `agents` comes back empty and `dm` warns the sender their message
  # will be "read by nobody", which sent a peer looking for a corpse while the
  # listener was working perfectly. `--back-in` is the deadline, so an empty
  # roster can be told from one that is merely between turns. Announcing does
  # not touch the read cursor — `checkin` would drain the very message this
  # exists to hand over, which is why this is `announce` and not that.
  sb -q announce --task "parked on inbox${DEADLINE:+ until $UNTIL_ISO}" \
    --ttl "$TTL" ${DEADLINE:+--back-in "$((DEADLINE - $(date +%s)))"} >/dev/null 2>&1 \
    || echo "wake-on-message: presence announce failed (pass $PASS)" >&2

  # The heartbeat says what this listener is doing *and* when it stops. A peer
  # reading it learns both halves of the question it actually has: will this
  # agent notice me, and if not now, when.
  sb -q board set "$HEARTBEAT" \
    "{\"pass\": $PASS, \"pid\": $$, \"waiting_on\": \"inbox\"${DEADLINE:+, \"until\": \"$UNTIL_ISO\", \"until_source\": \"$DEADLINE_SOURCE\"}, \"means\": \"this agent is parked and reachable; if this key is gone, nobody is listening and nothing will revive it\"}" \
    --json-body --ttl "$TTL" >/dev/null 2>&1 \
    || echo "wake-on-message: heartbeat write failed (pass $PASS)" >&2

  if MESSAGES="$(sb --json inbox --peek --wait "$WAIT" "$@" 2>/dev/null)"; then
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
