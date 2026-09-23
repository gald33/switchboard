---
name: doctor
description: >-
  Run the org's health checks with `org doctor`, gather session facts first when the sessions port is reachable, record which roles are dead or blocked and which checks could not run, stamp the operator file, and escalate. Use when the doctor wake fires or when asked whether the org is alive. Changes nothing; never prints an all-clear for checks that did not run. ---
---
<!-- org-core: role=doctor file=skill base=0.3.12 -->
<!-- org-core:base -->

# doctor

> **You are Doc.** Read `.claude/agents/doctor.md` for the role and its
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
(`git worktree add "$CLAUDE_SCRATCHPAD/doctor-wt" origin/main --detach`)
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
org tempo --repo /home/user/switchboard register --role doctor     # your agent id into coord/tempo/agents, so senders can wake you; answers `listener_alive` for step 6
switchboard --json inbox                                        # a DM naming a handoff key or a PR is why you were woken: act on that first
```

A DM is a wake, not a task list: do what it names, write your record, re-park
(step 6). An empty inbox means your cron fired: run the whole procedure.
`listener_alive: true` means the listener you parked last turn is still running
(your cron fired into a parked session): it wakes you when it exits, so step 6
parks nothing this turn.

## 1. Read what is already known

```bash
switchboard --json board get roles/doctor/last-run     # your own last record; absent = first run, the last one died, or it expired — a write after an expiry returns revision 1 exactly like a first write, so never read revision 1 as "never written"; say which you cannot tell
export SUITE="$(org setup --repo /home/user/switchboard status --compact)"     # the org-core and suite versions this container runs; {"report": "absent"} when the setup never ran here, empty when this line itself failed — the doctor's check 21 tells the three apart (row 91). `export`, not a `SUITE="$SUITE"` prefix: the `$(python3 …)` below is expanded before any prefix reaches a command, so the prefix form hands python an empty SUITE (NumeroTech 2026-09-21, row 112; measured by running the rendered block). Run both lines in ONE shell: an `export` made in one Bash call is gone in the next
switchboard --json board set roles/doctor/last-run "$(python3 -c '
import json, datetime, os
print(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "by": "Doc", "status": "in_progress", "blocked": None,
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

## 1b. Pin the repo you will measure

`/home/user/switchboard` is a shared checkout: any session may move its branch under
you, and a doctor that reads it measures whatever branch it was left on (a
doctor in Lucille found it 31 commits behind `origin/main` with no `org.yaml`
at all, 2026-09-19). Take your own tree at `origin/main` and
measure there:

```bash
cd /home/user/switchboard && git fetch origin main --quiet
BASE=$(git rev-parse origin/main)
export REPO="$CLAUDE_SCRATCHPAD/doctor-wt-$(date -u +%Y%m%dT%H%M)"   # exported: the steps below read it; unique, so a stale worktree from an earlier run is never measured
git worktree remove --force "$REPO" 2>/dev/null; git worktree prune
git worktree add "$REPO" "$BASE" --detach     # never `git checkout` in the shared dir
test "$(git -C "$REPO" rev-parse HEAD)" = "$BASE" || echo "the worktree is not at $BASE — report this, do not improvise"
test -f "$REPO/org.yaml" || echo "no manifest at $BASE — report this, do not improvise"
```

Use `"$REPO"` for every `--repo` and `--manifest` below, and for step 5's PR.

## 2. Gather facts, if you can

The trigger and session checks need what only the sessions port can list.
Search your tool list by **suffix** — tools register under hashed names and a
literal-name miss is not proof of absence:

* anything ending in `__list_triggers` → save the result as `triggers`
* anything ending in `__list_sessions` → save the result as `sessions`
* then, for **every `persistent_session_id` the triggers name that is not in
  that listing**, call the tool ending in `__get_session` with that id and
  append its row to `sessions`. A listing returns the newest N, and a session
  a routine has been firing into for days is old by construction — so the org's
  persistent sessions are the ones it drops, and they are its largest numbers
  (NumeroTech 2026-09-21: the dispatcher's bound session at US$5,462 cumulative
  was in no listing this account could take). Without them check 11's total
  excludes the biggest spender and check 4 cannot tell a dead bound session
  from an unlisted one; both say so, by name, rather than printing a figure.

Write them to a facts file in your scratchpad (never a bare `/tmp` name — they
collide across sessions):

```bash
FACTS="$CLAUDE_SCRATCHPAD/doctor-facts.json"   # {"triggers": [...], "sessions": [...]}
```

If neither tool is present, do not improvise: those checks will report
`NEEDS_FACTS`, and step 4 records `sessions` under `could_not_reach`.

## 3. Gather the tempo facts, then run the doctor

Checks 14 and 19 read the org's speed (SPEC §13) from a facts file the doctor does not gather itself:

```bash
org tempo --repo "$REPO" gather --since "$(date -u -d '7 days ago' +%Y-%m-%dT00:00:00Z)" ${FACTS:+--facts "$FACTS"}
```

It writes `$REPO/.org/tempo/facts-<today>.json` (board records, pull requests, run ledgers, and the triggers you saved above). Exit 2 means a source could not be read — the file still exists and says which; report that with the checks, not instead of them. Commit the file in step 5; the history of the org's speed exists only in these files.


```bash
org doctor --repo "$REPO" --manifest "$REPO/org.yaml" ${FACTS:+--facts "$FACTS"} --json > "$CLAUDE_SCRATCHPAD/doctor.json"
org doctor --repo "$REPO" --manifest "$REPO/org.yaml" ${FACTS:+--facts "$FACTS"}
```

Check 7 in served mode runs `diff` only if your session already holds the store
credential the manifest names (`ports.work_store.credential`); otherwise it reads
the org's sync workflow, which is the gate that ran `diff`. Read that credential
if you hold it; never mint one for this check — a doctor that mints is the
standing grant the org's own gate refuses. Check 8 reports a claim; releasing it
is the holder's or the operator's act, never yours.

Exit 1 means something is DEAD or FAIL; exit 2 means a port was unreachable —
report that as a finding about the port, not as a pass. Read the final line:
*ran N of 21 checks*. N is part of your headline whenever it is not 21.

An absent record has three causes and only one is a dead role (Lucille,
2026-09-19): the role writes under a key that is not its manifest name (read the
key its own file declares — the doctor already does, from `report_key`); it
wrote with the MCP client and the record expired in a day (it reappears every
run and is absent between — read its previous runs before calling it dead); or
it genuinely never wrote. The second and third are indistinguishable from the
board — a write after an expiry returns `revision: 1` exactly like a first write
— so name the absence as the fact and say the cause is undetermined; never
promote `revision: 1` into "never written". An absence goes in your headline by
name, not into `could_not_reach`.

## 4. Write the record — every run

The board record, from the same JSON the doctor just wrote, so the two cannot disagree:

```bash
REC="$CLAUDE_SCRATCHPAD/doctor-record.json"
org record-from-doctor "$CLAUDE_SCRATCHPAD/doctor.json" --repo "$REPO" --runtime fresh > "$REC"
org record "$REC"                                   # exit 0 or the record is not written anywhere
switchboard --json board set roles/doctor/last-run "$(python3 -c '
import json, sys, datetime
r = json.load(open(sys.argv[1]))
print(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "by": r["name"],
  "checks_total": r["counts"]["checks_total"], "ran": r["counts"]["ran"], "bad": r["counts"]["bad"], "dead": r["counts"]["dead"],
  "blocked": r["blocked"], "could_not_reach": r["could_not_reach"], "suite": r.get("suite") or {}, "headline": r["headline"]}))' "$REC")" --ttl 604800
switchboard --json say ops "doctor (Doc): $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["headline"])' "$REC")" --ttl 86400
```

## 5. Ledger and stamp, in one PR

The durable half. The board record expires in seven days; the ledger line and
the operator file do not.

```bash
cd "$REPO"                                          # the worktree of step 1b, already at the origin/main you measured
git switch -c "claude/doctor-$(date -u +%Y%m%d-%H%M)"
mkdir -p .org/runs && cat "$REC" >> .org/runs/doctor.jsonl && echo >> .org/runs/doctor.jsonl
```

Never `git checkout -B` in `/home/user/switchboard` itself: it moves the branch under
every other session using that directory.

Your section of `docs/human/state-of-the-org.md` — `### Doc — doctor · daily 09:00Z`,
three lines: **last run**, **outcome** (the `ran N of 21` sentence plus the dead
and blocked roles by name), **blocked** — is rendered by the chair from the record
you wrote, on `main`, on its next wake (`org operator render`). Do not edit the
file from this branch: a stamp in a PR sits behind whatever merges before it, and
the file then said `never ran` for roles that ran daily (rows 65, 98). Write the
record; the file follows. This PR carries the ledger line and the facts only.

```bash
git add .org/runs/doctor.jsonl .org/tempo/
git commit -m "doctor $(date -u +%Y-%m-%d): $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["headline"])' "$REC")"
git push -u origin HEAD
gh api repos/gald33/switchboard/pulls -f title="doctor $(date -u +%Y-%m-%d)" -f head="$(git branch --show-current)" -f base=main \
  -f body="$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["headline"] + "\n\nblocked: " + str(r["blocked"]) + "\ncould_not_reach: " + ", ".join(r["could_not_reach"]))' "$REC")"
```

`gh` is REST-only in cloud sessions; `gh pr create` 403s. You never merge the
PR — the CEO does.

## 6. Escalate

A DEAD role and a BLOCKED role each get one sentence in the "Waiting on the
operator" part of the operator file if — and only if — nothing in the org can
act on it: a trigger that needs re-attaching in the web UI, a session that
needs restarting by a human, a credential nobody unattended may mint. Anything
a role can act on stays in your record for the CEO's next wake.

## 7. On a drill

If this run was started by `org verify --drill`, the record carries
`"drill": true` and the headline says which role was killed on purpose. A
drill that the doctor did not notice is a failed verify, not a quiet day.


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
