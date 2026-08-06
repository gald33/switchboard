# The coordination demo

One terminal, ~40 seconds, no setup. It's the worked example from
[`docs/coordination-protocol.md`](../docs/coordination-protocol.md) played out for real
against a live (throwaway) hub instead of described in prose.

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

## Why it looks the way it does

- **It's real, not staged.** Every command is the actual CLI against an actual hub — a throwaway
  SQLite file and a random port, spun up and torn down by the script. Nothing is mocked or
  pre-recorded. Run it twice and you get the same shape of output both times, with fresh
  timestamps and revisions.
- **alice's TTL is shortened (`--ttl 5`) on purpose.** Presence defaults to a 2-minute expiry — true
  to life, but too long to sit through in a 40-second recording. Shortening it is the only liberty
  taken with the protocol's real behavior; everything else is default.
- **Requires the server extras** (`fastapi`, `uvicorn`, `pydantic`) importable by `python3` — the
  same ones `pip install "agent-switchboard[server]"` installs. The script checks for these and
  tells you the fix if they're missing, rather than failing deep in a traceback.
- **Always runs this checkout's source**, not whatever `switchboard` happens to be on `$PATH` —
  so the demo reflects the repo you're standing in, including any local changes.

## Recording it

Anything that captures a terminal works — [asciinema](https://asciinema.org/), a plain screen
recording, or your terminal's own recorder. There's nothing timing-sensitive to coordinate: the
script paces itself.

```bash
asciinema rec -c "bash demo/run.sh" switchboard-demo.cast
```

Pass `DEMO_FAST=1` to skip the readability pauses — useful for confirming the script itself still
runs end to end after an edit, not for the recording.
