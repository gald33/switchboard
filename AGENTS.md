# Agents

## Ground truth (repoctx)

For any non-trivial task in this repo:

1. Call `repoctx.bundle(task)` before proposing a plan. Treat the result as authoritative.
2. Do not edit paths outside `edit_scope.allowed_paths` without calling `repoctx.scope(task)` and `repoctx.refresh(task, changed_files, current_scope)`.
3. Before declaring done: call `repoctx.validate_plan(task, changed_files)` and `repoctx.risk_report(task, changed_files)`. Run every command the validation plan returns; resolve every `hard`-severity risk.
4. If unsure whether a change violates a constraint, call `repoctx.authority(task)` — do not guess.

Every repoctx response includes `when_to_recall_repoctx` and `before_finalize_checklist`. Follow them.

### First-time setup (one-shot)

If `contracts/` and `docs/architecture/` only contain the scaffold (`README.md` + `example.md`), repoctx has no real ground truth to surface. Bootstrap it once:

1. Call `repoctx.propose_authority()`. It returns `agent_brief` (markdown instructions), `suggested_files` (concrete paths to write), and detected `subsystems` + `contract_candidates`.
2. Author each file in `suggested_files` using the `agent_brief` conventions. Read 2–3 sample files per subsystem first — describe what *is* true, not what *should* be true.
3. Re-run `repoctx.bundle("sanity check")` to confirm the new authority loads.

### Embedding upkeep

After every Edit / Write / MultiEdit on a tracked source file, run:

    repoctx update <relative-path>

The command queues the path and auto-flushes (debounced — defaults to every 10 edits or 5 minutes). It is cheap to call on every edit; if you forget, the next `repoctx.bundle` / `repoctx.scope` call also flushes pending updates before reading the index, so stale vectors are bounded by your read cadence.

For bulk catch-up (after a rebase, branch switch, or external edits) prefer `repoctx index --incremental` — it re-embeds only chunks whose `content_hash` changed, much cheaper than a full rebuild. Use `repoctx rebuild` if the index is missing or you suspect corruption. `repoctx update --status` shows the queue; `repoctx update --flush` forces an immediate flush.


<!-- repoctx-nudge:v2 -->
> **repoctx is installed for this repo.** For any non-trivial task you
> **must call** `mcp__repoctx__bundle(task)` before proposing a plan, and
> `mcp__repoctx__validate_plan` + `mcp__repoctx__risk_report` before
> declaring done. Use `mcp__repoctx__authority(task)` if unsure whether
> a change violates a constraint.
>
> **Non-trivial = touches >1 file OR introduces new behavior OR
> adds/removes a public API.** Single-file typo/rename/comment-only
> changes are trivial.
