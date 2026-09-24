# State of the org

## Waiting on the operator

> **Resolved: production hub outage, 2026-09-24 about 18:25–18:31Z.** `switchboard.lucille-ai.com` answered Cloudflare 521, then 502, and `/health` returned 200 from 18:31:28Z. At 18:33Z the CEO confirmed board reads and the inbox work, and board records survived. No deploy caused it (the last hub deploy was 2026-09-23 11:05Z), so the cause is on `lucille-vm` and unknown from here. Worth a look at the VM's logs for what restarted around 18:25Z.

Kept by Link (ceo). One line per item; a role's own evidence is linked from its section below.

1. **Every role is procedure-less.** All 10 roles have an empty `org-core:project` section in both `.claude/skills/<role>/SKILL.md` and `.claude/agents/<role>.md` (20 files, 0 lines each; read 2026-09-23 12:47Z, after the install in #299). Each base says not to improvise, so eight roles have written `blocked` on it. Writing them is a role-text change — yours. *(raised 2026-09-23)*
2. **The ticket label does not exist.** `org.yaml` names tickets as GitHub issues labelled `ticket`; the account-manager read that label as not found in `gald33/switchboard` (2026-09-23, not re-checked by the CEO). The ticket queue is unreadable, not empty. Create the label or point `org.yaml` at the one you use. *(raised 2026-09-23)*
3. **`gh` is not installed in role containers.** The CEO container has no `gh` (2026-09-23 12:55Z); the dispatcher reports tempo cannot read PRs, so no reviewer was spawned for #301 (held since 12:53Z). Add it to the environment's setup script. *(raised 2026-09-23)*
4. **checks-watch has no registry.** `org.yaml` says `ports.checks.tool: absent`, yet checks-watch has a daily routine. Declare a registry, or set checks-watch `enabled: false` (Gus proposes the latter). *(raised 2026-09-23)*
5. **Schedules and cadences disagree.** Six routines were re-scheduled together on 2026-09-23 at 18:36Z: the dispatcher to every 6 h, and verifier, checks-watch, doctor, registrar and account-manager to Mondays and Thursdays. `org.yaml` declares no cadence, so the contracts still say hourly and daily, and the doctor will read these roles as dead. Set `cadence` in `org.yaml` to match, or restore the crons. *(raised 2026-09-24)*
6. **Two org-core 0.31.5 defects to file upstream** (org-core is not reachable from this org's sessions):
   - `org tempo register` answers `listener_alive: true` after a container restart when no listener process exists. Reproduced by the CEO at 2026-09-24 01:41Z (`ps`: 0) and, separately, by the verifier at about 07:10Z (`pgrep`: none). A persistent role that trusts it stays unreachable until its cron.
   - `org operator render` raises `FileNotFoundError` on an org's first render: it reads a missing operator file as empty but never creates `docs/human/`. *(raised 2026-09-24)*

Resolved: routines for doctor, verifier and checks-watch were created 2026-09-23 18:13Z (the doctor's 12:52Z line below predates that). There is still no CEO clock floor; it is reached by DM only.


### Link — ceo · a session, listening

- **last run:** 2026-09-24 01:42Z
- **outcome:** New: six switchboard routines were throttled (dispatcher 6-hourly, five roles Mon/Thu) at 18:36Z yesterday but org.yaml still implies hourly/daily — the doctor will call them dead; align one or the other. Four earlier items still open.
- **blocked:** no procedure for this org yet — all 10 roles have an empty org-core:project section (20 files). Writing them is a role_text_change: the operator decides. PR #301 not merged: no CEO procedure, no reviewer verdict.


### Marshal — dispatcher · hourly

- **last run:** 2026-09-24 06:22Z
- **outcome:** Spawned 0 — no dispatch procedure; routine fires every 6 h, not hourly; PR #301 unreviewed (gh missing); session at a 7-day rate-limit warning
- **blocked:** (1) no procedure for this org yet — dispatcher project section empty on origin/main d1b4e62 (operator queue item 1); (2) PR #301 reviewer held since 2026-09-23 12:53 — gh not installed here, tempo cannot read PRs (operator queue item 3); (3) my routine trig_01Py8BgoxMH12Tet7wAbvoMR fires every 6 h (`20 */6 * * *`, read 2026-09-24 00:21Z; this wake 06:21Z is consistent with it) while the role contract says hourly and the doctor reads DEAD past 2 h — set it back to `20 * * * *` or change the contract cadence


### Vera — verifier · daily 05:20Z

- **last run:** 2026-09-24 07:10Z
- **outcome:** Nothing was due: 0 verifying items of 41 on origin/main e5bff09 (24 ready, 12 done, 5 deferred); still blocked on the missing verifier procedure (operator item 1).
- **blocked:** no procedure for this org yet — project section of .claude/skills/verifier/SKILL.md and .claude/agents/verifier.md still empty on origin/main e5bff09 (read 2026-09-24 05:21Z); already operator item 1 in docs/human/state-of-the-org.md, so no new DM to ceo. docs/ai/ceo-watchlist.md still absent on main. | FINDING for the chair to file: after a container restart (2026-09-24 ~07:10Z) `org tempo register --role verifier` answered listener_alive: true while pgrep showed no listen process; I re-parked by hand. Any persistent role that trusts that answer after a restart parks nothing and stays unreac…


### Gus — checks-watch · daily 06:45Z

- **last run:** 2026-09-24 06:45Z
- **outcome:** Second run blocked the same way: org.yaml ports.checks.tool is absent on main, so checked=0 is not a clean reading. checks-watch should be enabled: false until a registry exists — operator decision.
- **blocked:** no checks port declared


### Nora — registrar · daily 07:04Z

- **last run:** 2026-09-24 07:02Z
- **outcome:** Still no registrar procedure (operator item 1); 0 of 41 items in verifying (24 ready, 12 done, 5 deferred), so nothing is due. Note: after a container restart on 09-24, tempo register answered listener_alive: true with no listener process running — check 4 can read a dead listener as alive.
- **blocked:** no procedure for this org yet — the project section of .claude/skills/registrar/SKILL.md is still empty at origin/main d1b4e62 (org-core 0.31.5, base 0.3.3); already on the operator list as item 1


### Ruth — account-manager · daily 07:15Z

- **last run:** 2026-09-23 12:48Z
- **outcome:** Blocked: no account-manager procedure for this org, and the ticket label named in org.yaml does not exist — the ticket queue cannot be read, not found empty.
- **blocked:** no procedure for this org yet: the project sections of .claude/skills/account-manager/SKILL.md and .claude/agents/account-manager.md are empty, so per the skill I did not improvise. Also: org.yaml names tickets as github-issues label "ticket", but that label does not exist in gald33/switchboard (get_label 2026-09-23: not found), so the open-ticket count of 0 is unreadable, not an empty queue.


### Ida — methods · weekly, Wednesdays 08:06Z

- **last run:** 2026-09-23 12:49Z
- **outcome:** methods has no org procedure yet: the operator needs to write the project section of .claude/skills/methods/SKILL.md before a review can run
- **blocked:** no procedure for this org yet — the org-core:project section of .claude/skills/methods/SKILL.md is empty on origin/main (e5bff09, org-core 0.31.5 install); the base step 2 says not to improvise


### Doc — doctor · daily 09:00Z

- **last run:** 2026-09-23 12:52Z
- **outcome:** ran 19 of 21 checks; 11 bad (1 not implemented, 1 unreachable: gh not on PATH). Tool says DEAD: verifier, checks-watch — CORRECTED by Doc: the tool matched Lucille's routines (trig_01UtUAp8…, trig_01RyrKoo…, same names Vera/Gus) to this org; re-derived, switchboard has NO trigger for verifier, checks-watch or doctor, so those two never ran here (not 'ran and could not report'). 4 roles BLOCKED on…
- **blocked:** operator: switchboard has no routine for verifier, checks-watch or doctor (org-core matched Lucille's same-named triggers, so check 1/3 misread them); 4 roles blocked on empty project procedure sections; operator file missing