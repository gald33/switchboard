#!/usr/bin/env bash
# demo/chess.sh — two agents, one channel, live at the same time.
#
# demo/run.sh shows the *async* half of the coordination protocol: alice
# hands off to the blackboard and her session ends; beta reads it two hours
# later with no memory of her. This one shows the other half — rule 4, live
# waits when both sides are actually active. "tal" and "beta" — sorry,
# "petrosian" — stay present on the `chess` channel for the whole exchange,
# trading moves with `say` + `inbox --wait`, the way two coding agents
# working the same PR in real time would trade status.
#
# The transcript below is not invented. It's a real game: two independent
# Claude sessions (one spawned via the Agent tool) actually played it out
# over a live switchboard hub, move by move, each tracking the board on its
# own side. Nothing here was rewritten for effect — including the part where
# black offers what it thinks is an even trade, white points out nothing
# actually defends the square, and black checks its own analysis and resigns
# two moves later. That's rule-of-thumb coordination-protocol material: the
# board state disagreement got caught and corrected on the channel it was
# announced on, not silently.
#
# One real wrinkle survived the replay on purpose: petrosian's attempt to
# use the encrypted blackboard failed in its sandbox (entering the workspace
# key on the command line got blocked as key-like input), so it fell back to
# tracking the position on its own side and coordinating purely through
# channel messages — which is exactly the kind of degraded-but-still-working
# path the protocol is supposed to leave open.
#
#   bash demo/chess.sh
#
# Set DEMO_FAST=1 to skip the readability pauses (for smoke-testing the
# script itself rather than watching it).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

switchboard() { PYTHONPATH="$REPO_ROOT/src" python3 -m switchboard.cli "$@"; }

if ! PYTHONPATH="$REPO_ROOT/src" python3 -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
  echo "missing server deps — run: pip install -e '$REPO_ROOT[server]'" >&2
  exit 1
fi

PAUSE_MOVE=0.32
PAUSE_BEAT=0.65
PAUSE_CLIMAX=1.5
if [ "${DEMO_FAST:-0}" = "1" ]; then
  PAUSE_MOVE=0.02
  PAUSE_BEAT=0.02
  PAUSE_CLIMAX=0.02
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
tal_color()  { printf '\033[36m%s\033[0m' "$1"; }   # cyan
pet_color()  { printf '\033[35m%s\033[0m' "$1"; }   # magenta

banner() {
  echo
  bold "── $1 ──"
  echo
  sleep "$PAUSE_BEAT"
}

step() {
  local display="$1"; shift
  printf '\n'
  dim '$ '
  printf '%s\n' "$display"
  sleep "$PAUSE_MOVE"
  "$@"
  sleep "$PAUSE_BEAT"
}

# speak <agent_id> <color_fn> <message...> — post to the chess channel as
# the given persona, tagged so it's obvious who's talking.
speak() {
  local agent="$1" color="$2"; shift 2
  export SWITCHBOARD_AGENT_ID="$agent"
  printf '\n'
  "$color" "$agent"
  printf ' '
  dim '$ switchboard say chess'
  printf ' "%s"\n' "$*"
  sleep "$PAUSE_MOVE"
  switchboard say chess "$*" >/dev/null
  sleep "$PAUSE_BEAT"
}

# board_note <fen> — tal posting the resulting position to the blackboard.
# Shown once to establish the pattern; run silently afterward so the
# transcript stays about the conversation, not fifteen repeats of the same
# command shape.
board_note() {
  export SWITCHBOARD_AGENT_ID=tal
  switchboard board set game/board "$1" >/dev/null
}

# --- infra: a throwaway hub, not part of the story --------------------------
PYTHONPATH="$REPO_ROOT/src" python3 -m switchboard.cli --token "$TOKEN" \
  serve --host 127.0.0.1 --port "$PORT" \
  --db "$WORKDIR/hub.db" --log-level warning \
  > "$WORKDIR/hub.log" 2>&1 &
HUB_PID=$!

export SWITCHBOARD_URL="http://127.0.0.1:$PORT"
export SWITCHBOARD_TOKEN="$TOKEN"
export SWITCHBOARD_WORKSPACE="chess-demo"

for _ in $(seq 1 100); do
  switchboard health >/dev/null 2>&1 && break
  sleep 0.1
done

# --- both agents register and stay present -----------------------------------
banner "TWO AGENTS, BOTH LIVE — chess channel"

export SWITCHBOARD_AGENT_ID=tal
step 'switchboard register --kind local -c chess --name tal --task "playing chess as Mikhail Tal"' \
  switchboard register --kind local -c chess --name tal --task "playing chess as Mikhail Tal"

export SWITCHBOARD_AGENT_ID=petrosian
step 'switchboard register --kind local -c chess --name petrosian --task "playing chess as Tigran Petrosian"' \
  switchboard register --kind local -c chess --name petrosian --task "playing chess as Tigran Petrosian"

step 'switchboard agents' \
  switchboard agents

dim '  … unlike the async demo, neither of them goes away. Both stay on the'
printf '\n'
dim '     roster for the whole game — this is the live-wait case, rule 4.'
printf '\n'
sleep "$PAUSE_BEAT"

banner "THE GAME"

speak tal tal_color "tal here — I'd rather be wrong and interesting than right and dull. White to move."
speak petrosian pet_color "petrosian here — no sacrifices without a receipt. Good luck, tal."

speak tal tal_color "e4"
export SWITCHBOARD_AGENT_ID=tal
step 'switchboard board set game/board "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"' \
  switchboard board set game/board "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
dim '  … tal does this after every move. Not shown again — same shape each time.'
printf '\n'
sleep "$PAUSE_MOVE"
speak petrosian pet_color "e5 — classical, solid, no weaknesses yet"

speak tal tal_color "Nf3"
board_note "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
speak petrosian pet_color "note: board blackboard (game/board) needs a workspace encryption key I can't safely enter here (sandbox blocks key-like strings on the command line) — I'll track the FEN on my end and rely on move notation in this channel instead. Continuing with the game."
speak petrosian pet_color "Nc6 — defending e5, developing"

speak tal tal_color "Bc4 — Italian. Let's see if you defuse it or I get to sac something."
board_note "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
speak petrosian pet_color "Bc5 — Giuoco Piano, I'll keep this quiet"

speak tal tal_color "b4 — Evans Gambit. Quiet is a choice, not a guarantee."
board_note "r1bqk1nr/pppp1ppp/2n5/2b1p3/1PB1P3/5N2/P1PP1PPP/RNBQK2R b KQkq b3 0 4"
speak petrosian pet_color "Bb6 — I'll decline, thanks. Keeping the structure clean"

speak tal tal_color "a4 — pushing. a5 next kicks your bishop, so something has to give."
board_note "r1bqk1nr/pppp1ppp/1bn5/4p3/PPB1P3/5N2/2PP1PPP/RNBQK2R b KQkq a3 0 5"
speak petrosian pet_color "a6 — stopping a5 before it starts"

speak tal tal_color "b5 — the knight has to move. axb5 axb5 just opens the a-file on my rook, so I don't mind that either."
board_note "r1bqk1nr/1ppp1ppp/pbn5/1P2p3/P1B1P3/5N2/2PP1PPP/RNBQK2R b KQkq - 0 6"
speak petrosian pet_color "axb5 — I'll take, and deal with the open file when it matters"

speak tal tal_color "Bxb5 — bishop retakes, not the pawn. axb5 there would open the a-file onto my own undefended rook, so this keeps development and still hits c6."
board_note "r1bqk1nr/1ppp1ppp/1bn5/1B2p3/P3P3/5N2/2PP1PPP/RNBQK2R b KQkq - 0 7"
speak petrosian pet_color "Nf6 — developing, eyeing e4"

speak tal tal_color "O-O — tucking the king in. If you grab e4 I've got Re1 and d4 coming, and I like my chances in the mess more than you will."
board_note "r1bqk2r/1ppp1ppp/1bn2n2/1B2p3/P3P3/5N2/2PP1PPP/RNBQ1RK1 b kq - 3 8"
speak petrosian pet_color "O-O — I'll leave e4 alone, king safety first"

speak tal tal_color "d4 — striking the center now that we're both castled."
board_note "r1bq1rk1/1ppp1ppp/1bn2n2/1B2p3/P2PP3/5N2/2P2PPP/RNBQ1RK1 b - d3 0 9"
speak petrosian pet_color "exd4 — simplest, I'll take the trade"

speak tal tal_color "Nxd4 — recapturing, centralizing, and eyeing c6."
board_note "r1bq1rk1/1ppp1ppp/1bn2n2/1B6/P2NP3/8/2P2PPP/RNBQ1RK1 b - - 0 10"
speak petrosian pet_color "Nxd4 — trading, simplifying"

speak tal tal_color "Qxd4 — queen recaptures and centralizes. Material's level, I just like my position better."
board_note "r1bq1rk1/1ppp1ppp/1b3n2/1B6/P2QP3/8/2P2PPP/RNB2RK1 b - - 0 11"
speak petrosian pet_color "c5 — kicking the queen, gaining space"

speak tal tal_color "Qd3 — retreating. Qxc5 was there but Bxc5 just wins my queen for a bishop, so no thanks."
board_note "r1bq1rk1/1p1p1ppp/1b3n2/1Bp5/P3P3/3Q4/2P2PPP/RNB2RK1 b - - 1 12"
speak petrosian pet_color "d5 — striking back in the center"

speak tal tal_color "exd5 — opening it up further. My pieces like open lines more than yours do right now."
board_note "r1bq1rk1/1p3ppp/1b3n2/1BpP4/P7/3Q4/2P2PPP/RNB2RK1 b - - 0 13"
speak petrosian pet_color "Nxd5 — recapturing, knight's well placed here"

speak tal tal_color "Nc3 — last minor piece out, and it leans on your d5 knight."
board_note "r1bq1rk1/1p3ppp/1b6/1Bpn4/P7/2NQ4/2P2PPP/R1B2RK1 b - - 1 14"
speak petrosian pet_color "Nxc3 — trading again, I like fewer pieces on the board"

speak tal tal_color "Qxc3 — recapturing onto the long diagonal. Nothing concrete yet, just pointed at your king now."
board_note "r1bq1rk1/1p3ppp/1b6/1Bp5/P7/2Q5/2P2PPP/R1B2RK1 b - - 0 15"
speak petrosian pet_color "Qf6 — same diagonal, happy to trade queens if you want them"

speak tal tal_color "Qc4 — declining. I've got nothing without queens on, so I'm sidestepping the trade and leaning on f7 instead."
board_note "r1b2rk1/1p3ppp/1b3q2/1Bp5/P1Q5/8/2P2PPP/R1B2RK1 b - - 2 16"
speak petrosian pet_color "Be6 — develops, blocks the diagonal, and hits your queen"

speak tal tal_color "Qd3 — retreating, keeping it simple. That diagonal's yours now, fine, I'll find another one."
board_note "r4rk1/1p3ppp/1b2bq2/1Bp5/P7/3Q4/2P2PPP/R1B2RK1 b - - 4 17"
speak petrosian pet_color "Rad8 — taking the open file"

banner "THE BOARD-STATE MOMENT"

speak tal tal_color "Qe3 — had to move, your rook was staring straight down the d-file at my queen. cxd3 would've recaptured the rook but I'd still be down queen-for-rook, so off it goes instead."
board_note "3r1rk1/1p3ppp/1b2bq2/1Bp5/P7/4Q3/2P2PPP/R1B2RK1 b - - 6 18"
speak petrosian pet_color "Bc4 — offering the trade, happy to simplify further"

sleep "$PAUSE_CLIMAX"
speak tal tal_color "Bxc4 — taking it. I don't see a black piece that was actually covering c4 (Bb6 doesn't reach it, Qf6 isn't on that line), so this just wins the bishop outright rather than trading it — flag it if I'm missing a recapture."
board_note "3r1rk1/1p3ppp/1b3q2/2p5/P1B5/4Q3/2P2PPP/R1B2RK1 b - - 0 19"

sleep "$PAUSE_CLIMAX"
speak petrosian pet_color "no, you're right — I miscounted, nothing covers c4. That's a clean bishop for you, no complaints. Down a piece now, I'll play on but that stings."

sleep "$PAUSE_CLIMAX"
speak petrosian pet_color "I resign. A clean piece down with no compensation in an otherwise level position isn't something to grind out — well played, tal. Good game."

speak tal tal_color "Good game, petrosian — that Bc4 slip aside, this was a real Evans Gambit, not a script. GG."

# --- end on the shared record, not a feature list ----------------------------
banner "FULL RECORD — both agents agree on this"
step 'switchboard history chess --limit 100' \
  switchboard history chess --limit 100
