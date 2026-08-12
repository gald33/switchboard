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

Three verdicts, and the third is the one the tool exists for:

| | Meaning |
|---|---|
| `passed` | reported, and said it succeeded |
| `failed` | reported, and said it failed — the task was tested |
| `silent` | never reported. The *coordination* failed, whatever the process exit code says |

`failed` and `silent` are kept apart deliberately. Collapsing them into
"not ok" loses which half of the system broke.

Exit code is 0 only if every worker passed. The full report is JSON with
`--json`, written to a file with `--out`, and always left on the
blackboard at `drill/<run>/report`, so a peer agent can read the result
of a drill it did not run.

## Workers

| `--worker` | What it is |
|---|---|
| `auto` (default) | `claude` when that binary is on `PATH`, else `builtin` |
| `claude` | a real `claude -p` session given the protocol in its prompt |
| `builtin` | a scripted agent inside Switchboard that walks the same protocol in under a second |
| `custom` | your own command, via `--worker-cmd` (`{slot}` and `{run_id}` are substituted) |

`builtin` is what CI runs: same registration, same lease, same blackboard
write, no model, no tokens, deterministic. `claude` is the real test.
Asking for `claude` without the binary is an error rather than a silent
downgrade — a report that says "passed" about workers that were never the
ones you asked for is worse than no report.

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
- `--dir` — the working directory the workers are started in.
- `--model` — for `--worker claude`.

Drills work on a sealed workspace, where agent ids, channels, lease
resources and board keys are all blinded before they leave the client.
The report's lease resources are the blinded tokens there, because that is
genuinely all anyone has; every other number stays true.
