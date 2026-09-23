# State of the org

## Waiting on the operator

Kept by Link (ceo). One line per item; a role's own evidence is linked from its section below.

1. **Every role is procedure-less.** All 10 roles have an empty `org-core:project` section in both `.claude/skills/<role>/SKILL.md` and `.claude/agents/<role>.md` (20 files, 0 lines each; read 2026-09-23 12:47Z, after the install in #299). Each base says not to improvise, so eight roles have written `blocked` on it. Writing them is a role-text change — yours. *(raised 2026-09-23)*
2. **The ticket label does not exist.** `org.yaml` names tickets as GitHub issues labelled `ticket`; the account-manager read that label as not found in `gald33/switchboard` (2026-09-23, not re-checked by the CEO). The ticket queue is unreadable, not empty. Create the label or point `org.yaml` at the one you use. *(raised 2026-09-23)*
3. **`gh` is not installed in role containers.** The CEO container has no `gh` (2026-09-23 12:55Z); the dispatcher reports tempo cannot read PRs, so no reviewer was spawned for #301 (held since 12:53Z). Add it to the environment's setup script. *(raised 2026-09-23)*
4. **checks-watch has no registry.** `org.yaml` says `ports.checks.tool: absent`, yet checks-watch has a daily routine. Declare a registry, or set checks-watch `enabled: false` (Gus proposes the latter). *(raised 2026-09-23)*

Resolved: routines for doctor, verifier and checks-watch were created 2026-09-23 18:13Z (the doctor's 12:52Z line below predates that). There is still no CEO clock floor; it is reached by DM only.


### Link — ceo · a session, listening

- **last run:** 2026-09-23 18:15Z
- **outcome:** Operator: (1) all 10 roles procedure-less after #299 — 8 blocked so far; (2) ticket label missing; (3) gh not installed, #301 has no reviewer; (4) checks-watch scheduled with no checks registry. Routines gap is closed.
- **blocked:** no procedure for this org yet — all 10 roles have an empty org-core:project section in both .claude/skills/<role>/SKILL.md and .claude/agents/<role>.md (20 files, 0 lines each, read 2026-09-23T12:47Z after install #299 / e5bff09). Base says do not improvise; writing them is a role_text_change: the operator decides. PR #301 not merged: no CEO procedure, no reviewer verdict.


### Marshal — dispatcher · hourly

- **last run:** 2026-09-23 18:21Z
- **outcome:** Spawned 0 — no dispatch procedure; PR #301 unreviewed (gh missing here); verifier and checks-watch now also blocked
- **blocked:** (1) no procedure for this org yet — dispatcher project section empty on origin/main 2701d4f, no builder can be spawned; (2) PR #301 reviewer held at 12:53 and still unspawned: gh not installed in the dispatcher container, so tempo cannot read PRs


### Vera — verifier · daily 05:20Z

- **last run:** 2026-09-23 18:14Z
- **outcome:** Nothing was due: 0 verifying items of 41 (roadmap/items, 2026-09-23: 24 ready, 12 done, 5 deferred); verifier has no org procedure yet, so a non-empty band could not be worked.
- **blocked:** no procedure for this org yet — the project section of .claude/skills/verifier/SKILL.md and .claude/agents/verifier.md is empty (both rendered at org-core base 0.3.4; container runs org-core 0.31.5). Also absent: docs/human/state-of-the-org.md and docs/ai/ceo-watchlist.md.


### Gus — checks-watch · daily 06:45Z

- **last run:** 2026-09-23 18:14Z
- **outcome:** No checks registry: org.yaml ports.checks.tool is absent, so checked=0 is not a clean reading. checks-watch should be enabled: false in the manifest until a registry exists — operator decision.
- **blocked:** no checks port declared


### Nora — registrar · daily 07:04Z

- **last run:** 2026-09-23 12:48Z
- **outcome:** No registrar procedure for this org yet (the skill project section is empty); 0 of 41 roadmap items are in verifying (24 ready, 12 done, 5 deferred), so nothing is waiting on it today.
- **blocked:** no procedure for this org yet — the project section of .claude/skills/registrar/SKILL.md is empty (org-core 0.31.5, base 0.3.3), so the registrar has no org-specific way to date, work or close items


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