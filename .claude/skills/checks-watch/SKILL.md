---
name: checks-watch
description: >-
  Read the production checks registry read-only and report what is red, what expired while red, what passes vacuously, and what fails with no owner. Use when the checks-watch sweep fires, when asked what production assertions are failing, or before treating a green check as proof. Reports only; never fixes, never files, never reads a pass with all-zero diagnostics as a pass. ---
---
<!-- org-core: role=checks-watch file=skill base=0.3.7 -->
<!-- org-core:base -->

# checks-watch

> **You are Gus.** Read `.claude/agents/checks-watch.md` for the role and its
> limits. This is how a run goes. The base steps are rendered from the contract;
> this org's detailed procedure — if it has one — is in the project section below,
> and it decides *how*, never *whether*.

## 0. Pin the workspace before any board call

```bash
cd /home/user/switchboard
export SWITCHBOARD_WORKSPACE=gald33/switchboard
switchboard --json whoami        # confirm: "workspace": "gald33/switchboard"
```

A report in the wrong room is indistinguishable from no report, and the CLI
reports success either way.

Then `git -C /home/user/switchboard status --short`, before you touch the checkout:
in-process subagents and other sessions share it (NumeroTech, 2026-09-17: a
methods run found the builder mid-build there — eleven modified files minutes
after a clean status). If anything you did not modify is modified, do not branch
or commit there: take your own worktree
(`git worktree add "$CLAUDE_SCRATCHPAD/checks-watch-wt" origin/main --detach`)
and branch from `origin/main`, never from the branch you found.

The work store is files with a gitignored local store, so a cold container reads
an **empty** store at exit 0 — `ready` says "nothing ready", `list` says "no
items", `validate` says "ok — 0 items" — and every role reads its own false
finding: no item answers this ticket, a quiet day, an empty verifying band
(NumeroTech, 2026-09-17, in a clean clone with 24 item files). Seed it from the
files before any read, once your checkout is the branch you mean:

```bash
roadmap push && roadmap doctor     # seed, then check; only then read the queue
```

## 0b. You are a persistent agent: register, then read your inbox

```bash
org tempo --repo /home/user/switchboard register --role checks-watch     # your agent id into coord/tempo/agents, so senders can wake you; answers `listener_alive` for step 6
switchboard --json inbox                                        # a DM naming a handoff key or a PR is why you were woken: act on that first
```

A DM is a wake, not a task list: do what it names, write your record, re-park
(step 6). An empty inbox means your cron fired: run the whole procedure.
`listener_alive: true` means the listener you parked last turn is still running
(your cron fired into a parked session): it wakes you when it exits, so step 6
parks nothing this turn.

## 1. Read what is already known

```bash
switchboard --json board get roles/checks-watch/last-run     # your own last record; absent = first run, the last one died, or it expired — a write after an expiry returns revision 1 exactly like a first write, so never read revision 1 as "never written"; say which you cannot tell
export SUITE="$(org setup --repo /home/user/switchboard status --compact)"     # the org-core and suite versions this container runs; {"report": "absent"} when the setup never ran here, empty when this line itself failed — the doctor's check 21 tells the three apart (row 91). `export`, not a `SUITE="$SUITE"` prefix: the `$(python3 …)` below is expanded before any prefix reaches a command, so the prefix form hands python an empty SUITE (NumeroTech 2026-09-21, row 112; measured by running the rendered block). Run both lines in ONE shell: an `export` made in one Bash call is gone in the next
switchboard --json board set roles/checks-watch/last-run "$(python3 -c '
import json, datetime, os
print(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "by": "Gus", "status": "in_progress", "blocked": None,
  "suite": json.loads(os.environ.get("SUITE") or "{}"),
  "headline": "run started, not finished — if you are reading this, the session ended before step 3"}))')" --ttl 604800
```

Open the record **now**, before any work, and finish it at step 3. A record
written last is never written when the session ends first — a turn that runs
out, an operator who redirects you, a run that simply finishes its business —
and the end of a session has no prompt. Lucille measured the cost on
2026-09-19: the CEO's record had never existed, on an org four days old, while
every run had asked for it as its last step. An open record that step 3 never
overwrites is what the doctor reads as "ended before its last step" (check 1).

Then `docs/human/state-of-the-org.md` and the dated commitments in `docs/ai/ceo-watchlist.md`. Do not
re-derive what a previous run already established; read it. (A date or a count
you can re-read from its source in one call is a read, not a re-derivation —
re-read it; the watchlist is a projection of the source, never the source. The
same for a finding you are about to repeat or an operator bullet you carry
forward: re-read its premise from the source first — NumeroTech 2026-09-20
flagged two migrations "18 days behind" on four consecutive runs after the
operator had applied them.)

## 1. Pin the workspace before any board call

```bash
export SWITCHBOARD_WORKSPACE=gald33/switchboard
switchboard --json whoami        # confirm: "workspace": "gald33/switchboard"
```

A report in the wrong room is indistinguishable from no report, and the CLI
reports success either way.

## 2. Read your own last record first

```bash
switchboard --json board get roles/checks-watch/last-run
```

Your trend lines come from here. If there is no record, say so in today's —
"first run, or the last one died" — rather than starting the trend at today.

## 3. Read the registry, read-only

**This org declares no checks port** (`org.yaml` ports.checks is absent), so there
is no registry to read. Write a record with `blocked: "no checks port declared"`,
stamp the operator file, and stop — and say in the headline that this role
should be `enabled: false` until a registry exists.

## 4. Compute the reading

For every check: state (`pass`, `fail`, `pending`, `expired`, `inconclusive`,
`error`), severity, `check_after`, `until`, and its diagnostics.

* **Counts by state**, and `high` among the failures.
* **Oldest failure**: id and days red.
* **Failures without an owner**: for each failing check, `grep -l <check-id>
  roadmap/items/*.yaml`; none → unowned.
* **Vacuous passes**: every numeric diagnostic is 0 and the id is not in the
  registry's known-vacuous list. For each, write the one sentence: *if this
  feature were completely broken, this check would show ___.* If the blank is
  "the same", it is a finding.
* **Expiring within 7 days**, and **expired while red** since your last record.
* **Trend**: for each failure present in your last record, is its diagnostic
  better, worse or the same? A worsening failure is its own line.

## 5. Write the record — on every run, including a clean one

```bash
export SUITE="$(org setup --repo /home/user/switchboard status --compact)"     # the org-core and suite versions this container runs (doctor check 21)
switchboard --json board set roles/checks-watch/last-run "$(python3 -c '
import json, datetime, os
print(json.dumps({
  "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "by": "Gus",
  "suite": json.loads(os.environ.get("SUITE") or "{}"),
  "checked": 0, "pass": 0, "fail": 0, "high": 0, "pending": 0, "expired": 0, "inconclusive": 0, "error": 0,
  "oldest_failure": "<id>, red since <date>, <n> days",
  "failures_without_item": [],
  "vacuous_unflagged": [],
  "expired_while_red": [],
  "worsening": [],
  "blocked": None,
  "method": "<the exact read-only invocation used>",
  "headline": "one sentence a human should act on",
}))')" --ttl 604800
switchboard --json say ops "checks-watch (Gus): <fail> red (<high> high), <n> unowned, <m> vacuous — <headline>" --ttl 86400
```

`at` is the time you write it. A record whose `at` is ahead of the hub's own
timestamp is a run time that is not the run time, and the doctor flags it.

## 6. Your line in the operator file is rendered, not stamped

`docs/human/state-of-the-org.md` — `### Gus — checks-watch · daily 06:45Z`,
three lines: **last run**, **outcome**, **blocked** — is the one report the
operator reads without an agent relaying it, and the chair renders every role's
three lines from `roles/<role>/last-run` on `main` each wake. You have no path
to `main`; a stamp from your branch sits behind whatever merges before it, and
the file then said `never ran` for roles that ran daily (rows 65, 98). Write the
record; the file follows.

## 7. What you do not do

No fix, no item, no ticket, no status change. If a reading is worth an item,
say so in the headline; the roles that file and build read this record.


## 6. Re-park the listener before you end — every turn no listener is already running

```bash
switchboard listen --until "$(org tempo --repo /home/user/switchboard park-until)"    # 24 h; run it with your runner's background mechanism (run_in_background), never `&` or `nohup`
```

Its exit is your next wake: exit 0 a DM arrived (read `switchboard --json inbox`
and act), exit 2 the deadline passed (re-park — that expiry is your health tick,
one turn a day), exit 1 the hub was unreachable five times running (re-park, and
say so in your record). The runner labels any non-zero exit **`failed`**, so a
clean deadline comes back as `failed with exit code 2`: that is the designed
outcome, not a crash — re-park (Lucille, 2026-09-19: one reviewer misread it five
times and stopped re-parking). Run the command bare, never `…; echo $?`: a compound
command exits with its last command's status, so the wrapper reports 0 and the
code you wanted is gone. Measured 2026-09-19: the runner re-invoked the session
six seconds after the listener exited. One listener per session: when step 0b's
`register` answered `listener_alive: true`, the one parked last turn is still
running and will wake you — park nothing. A persistent role that ends a turn with
no listener running is unreachable until its cron, and the doctor reads it as not
listening (check 4).

<!-- org-core:project -->
<!-- org-core:end -->
