# Drills

A drill launches a few agents at one task and reports what the hub saw.

```bash
switchboard drill "summarise the three largest modules in src/" -n 3
```

It is a test of the coordination path as agents actually use it — not of
the client library, which the unit tests already cover. The coordinator
starts N workers, hands them a single brief through the blackboard, and
then learns everything else the way a peer agent would: presence,
messages, leases, blackboard. Worker stdout is discarded on purpose. If
the only way to find out whether an agent is alive is to read its
terminal, the hub failed at its job, and a report built partly from
stdout would hide exactly that failure.

## What comes back

```
PASS  drill 1786530200-a875b2  14s
task    summarise the three largest modules in src/

WORKER   VERDICT   ANNOUNCE   RESULT    MSGS  SUMMARY
w1       passed    1.0s       9.4s      2     4/4 checks passed
         ok brief-readable: read the brief from the blackboard
         ok peers-visible: saw 3/3 drill agent(s) on the hub
         ok own-slot-lease: acquired
         ok shared-lease: won the contended key
...
3 worker(s): 3 passed, 0 failed, 0 silent
announce latency  min 1.0s  mean 1.0s  max 1.1s   channel messages 6  hub polls 12
```

Four verdicts, and the last two are what the tool exists for:

| | Meaning |
|---|---|
| `passed` | reported, and said it succeeded |
| `failed` | reported, and said it failed — the task was tested |
| `no-report` | announced or spoke, then produced no result |
| `silent` | never heard from at all |

These are kept apart deliberately, and each names a different place to
look. `failed` means the task was exercised and found wanting; the hub did
its job. `silent` means nothing was ever heard from that worker — suspect
the launch, or the hub's ability to carry a presence. `no-report` sits
between them and is the easiest one to lose: the hub demonstrably *did*
carry this worker's word, so coordination worked, and the fault is in the
task, the worker, or the one key both sides have to agree on.

Collapsing `no-report` into `silent` is not a cosmetic loss. It sends
every investigation at the hub, half of them wrongly — which is exactly
how a worker that could not call a single tool, and a worker that did the
work and wrote it to a key nobody read, came back looking identical.

Exit code is 0 only if every worker passed. The full report is JSON with
`--json`, written to a file with `--out`, and always left on the
blackboard at `drill/<run>/report`, so a peer agent can read the result
of a drill it did not run.

## Workers

| `--worker` | What it is |
|---|---|
| `auto` (default) | `claude` when that binary is on `PATH`, else `builtin` |
| `claude` | a real `claude -p` session given the protocol in its prompt, reaching the hub through the CLI |
| `claude-mcp` | the same session reaching the hub through the MCP server instead |
| `builtin` | a scripted agent inside Switchboard that walks the same protocol in under a second |
| `custom` | your own command, via `--worker-cmd` (`{slot}` and `{run_id}` are substituted) |

`builtin` is what CI runs: same registration, same lease, same blackboard
write, no model, no tokens, deterministic. `claude` is the real test.
Asking for `claude` without the binary is an error rather than a silent
downgrade — a report that says "passed" about workers that were never the
ones you asked for is worse than no report.

`claude-mcp` exists because this package ships two agent surfaces and a
drill that only exercises one of them cannot notice the other going dark.
Its workers are given the MCP tools and no shell at all, so the run cannot
quietly fall back to the CLI and report a green MCP path it never used.
It needs `switchboard-mcp` on `PATH`, which is checked before launching
for the same reason the `claude` check exists: a worker whose MCP server
never starts has no hub tools, does nothing, and arrives in the report
looking like a worker that chose to stay quiet.

Whichever kind you pick, the coordinator hands its workers the hub,
workspace and key it resolved for itself. Left to themselves they would
each derive a workspace from their own working directory, so `--dir`
outside the coordinator's repo used to put them in a room where the brief
does not exist.

## The protocol

Every worker, whichever kind, does this:

1. register with the hub
2. say hello on `drill/<run>`
3. read the brief from `drill/<run>/brief`
4. claim `drill/<run>/<slot>` — its own slot, so it should win
5. do the task, running `--verify` if one was given
6. write `{"ok": …, "summary": …, "checks": […]}` to `drill/<run>/results/<agent>`
7. say done on the channel

Steps 2–7 are each visible from outside, which is what makes the run a
measurement rather than an impression.

The builtin worker adds one probe on top: after waiting for its peers to
appear, every worker reaches for the same `drill/<run>/contended` key.
Exactly one may hold it. That check is reported but never scored — losing
the race is the correct outcome for all but one worker, and scoring it
would fail a hub for behaving properly.

Workers do not deregister. A drill's presence and leases expire on their
own within a couple of minutes, which is both the project's own thesis
about claims and the reason the evidence is still there to look at when
the run ends.

## Options worth knowing

```bash
switchboard drill "port the auth tests to pytest" \
    -n 3 --verify "pytest -q tests/auth" --timeout 900 --out drill.json
```

- `--verify CMD` — a shell command each worker runs and reports on. The
  natural way to make a drill assert something about the repo rather than
  only about the hub.
- `--timeout` — when unreported workers are terminated. It is the hang
  path, not the normal one: a run ends as soon as every worker has either
  reported or exited.
- `--dir` — the working directory the workers are started in. It does not
  change which workspace they join; that is inherited from the coordinator.
- `--model` — for `--worker claude` and `--worker claude-mcp`.

A `custom` worker is told the run's keys through its environment —
`SWITCHBOARD_DRILL_CHANNEL`, `SWITCHBOARD_DRILL_BRIEF_KEY` and
`SWITCHBOARD_DRILL_RESULT_KEY`, alongside `SWITCHBOARD_DRILL_RUN`, `_SLOT`,
`_TASK` and `_VERIFY`. Use `SWITCHBOARD_DRILL_RESULT_KEY` verbatim rather
than rebuilding it: results are fetched by exact key, so a worker that
derives its own lands the write where the observer never looks and is
reported as having said nothing.

Drills work on a sealed workspace, where agent ids, channels, lease
resources and board keys are all blinded before they leave the client.
The report's lease resources are the blinded tokens there, because that is
genuinely all anyone has; every other number stays true.
