# The coordination demos

Two terminals, two different halves of [`docs/coordination-protocol.md`](../docs/coordination-protocol.md)
played out for real against a live (throwaway) hub instead of described in prose.

## Async handoff — `run.sh`

One terminal, ~40 seconds, no setup.

```bash
bash demo/run.sh
```

What you'll see:

1. **alice** (a local session) checks the blackboard, finds nothing, and posts a migration-order
   proposal to `coord/proposals/db-migration-order` — then points to it with a one-line message
   on `build`. Her session ends.
2. Time passes. **beta** (a cloud session, a different process with no memory of alice) starts up.
   The roster shows alice is gone — really gone, her presence actually expired, nothing staged —
   but the blackboard entry is still there.
3. beta reads it and makes the compatible call: takes migration `0143`, not the `0142` alice
   already has. It records that on the board too.
4. The demo ends on `board list --prefix coord/` — the state both agents now agree on, not a tour
   of every command Switchboard has.

alice's TTL is shortened (`--ttl 5`) on purpose — presence defaults to a 2-minute expiry, true to
life but too long to sit through in a 40-second recording. That's the only liberty taken with the
protocol's real behavior; everything else is default.

## Live coordination — `chess.sh`

One terminal, ~70 seconds. The other half of the protocol: rule 4, live waits when both sides are
actually active.

```bash
bash demo/chess.sh
```

**tal** (White) and **petrosian** (Black) both stay registered on the `chess` channel for the whole
exchange, trading moves with `say` + `inbox --wait` the way two agents working the same PR in real
time would trade status — never `board list --prefix coord/`-and-gone like alice.

The transcript isn't invented. Two independent Claude sessions actually played this game, live,
over a real hub — one spawned as a subagent, the other driving the CLI directly — each tracking
the board on its own side, with `chess.sh` replaying the exact real commands and messages
afterward so the recording is clean and reproducible. Nothing was rewritten for effect, including
the part where Black offers what it thinks is an even trade, White points out that nothing actually
defends the square, Black checks its own analysis, confirms the miscalculation, and resigns two
moves later — a board-state disagreement caught and corrected on the channel it was raised on,
which is the whole point of coordinating out loud instead of in a PR comment. One more real wrinkle
survived the replay on purpose: Black's attempt to use the encrypted blackboard failed in its
sandbox, so it fell back to tracking the position on its own side and coordinating purely through
channel messages — the degraded-but-still-working path the protocol is meant to leave open.

## Why they look the way they do

- **They're real, not staged.** Every command is the actual CLI against an actual hub — a throwaway
  SQLite file and a random port, spun up and torn down by the script. Nothing is mocked or
  pre-recorded. Run either one twice and you get the same shape of output both times, with fresh
  timestamps and revisions.
- **Requires the server extras** (`fastapi`, `uvicorn`, `pydantic`) importable by `python3` — the
  same ones `pip install "agent-switchboard[server]"` installs. Both scripts check for these and
  tell you the fix if they're missing, rather than failing deep in a traceback.
- **Always runs this checkout's source**, not whatever `switchboard` happens to be on `$PATH` —
  so the demo reflects the repo you're standing in, including any local changes.

## Recording it

Anything that captures a terminal works — [asciinema](https://asciinema.org/), a plain screen
recording, or your terminal's own recorder. There's nothing timing-sensitive to coordinate: both
scripts pace themselves.

```bash
asciinema rec -c "bash demo/run.sh" switchboard-demo.cast
asciinema rec -c "bash demo/chess.sh" switchboard-chess-demo.cast
```

Pass `DEMO_FAST=1` to skip the readability pauses on either script — useful for confirming the
script itself still runs end to end after an edit, not for the recording.
