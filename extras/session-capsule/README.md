# Claude Code session capsule (prototype)

Experiment log and disposable tooling for carrying a **native Claude Code
session** between environments and resuming it there by its original session
id. This is the "native handoff" mode (Claude Code -> Claude Code). It does
not touch Switchboard's source; the point was to prove portability first.

Everything below was observed on Claude Code **2.1.260** (Linux, the
`claude.ai/code` cloud container), by inspecting the on-disk state and the
CLI binary, then running the CLI against isolated config directories.
Statements are labelled **observed** (reproduced here) or **hypothesis**
(read from the binary or inferred, not exercised).

## TL;DR

**Observed:** a Claude Code session is portable as a single opaque file.
Copying `<session-id>.jsonl` into `<config-dir>/projects/<any-project-key>/`
on another installation, then running `claude --resume <session-id>`, restores
the full conversation: user turns, assistant turns, tool calls and tool
results, and facts that only ever existed as tool output. Verified with:

| Test | Result |
|---|---|
| Resume in a fresh, otherwise empty config dir | works |
| Resume from a different working directory than the one recorded in the file | works |
| File placed under the *original* project key (no path translation at all) | works (cross-project scan finds it) |
| File placed under the *destination* directory's project key | works (direct lookup) |
| Round trip A -> B -> A, with a fact added in B | works, B's fact visible in A |
| Session that spawned a subagent, moved with sidecar files | works |
| Same session, moved with the main file only | parent conversation still resumes |
| The real, live 537 KB session that ran this investigation | works |
| Same id present under two project keys, cwd matching neither | **fails**: "No conversation found" |

No transcript was modified. No path translation was needed. The destination
needs neither the repo path nor the repo contents to *resume the conversation*;
it needs them only for whatever the conversation does next.

## 1. Where Claude Code stores sessions (observed)

```
<config-dir>/                       $CLAUDE_CONFIG_DIR, else ~/.claude
  projects/
    <project-key>/                  cwd with every [^A-Za-z0-9] replaced by "-"
      <session-id>.jsonl            the transcript: one JSON record per line
      <session-id>/                 optional sidecar dir, per session
        subagents/agent-<id>.jsonl      a subagent's own transcript
        subagents/agent-<id>.meta.json  {"agentType","description","toolUseId","spawnDepth"}
        ccr-tip.json                    cloud-runner bookkeeping (remote sessions only)
  sessions/<pid>.json               live-process registry (pid, cwd, socket); not a session artifact
  .claude.json                      global config, oauth account, per-cwd project settings
```

Project key for this container's repo: `/home/user/switchboard` ->
`-home-user-switchboard`. The binary truncates keys over 200 characters and
appends a hash (**hypothesis**: seen in code, not exercised; the prototype
refuses such paths instead of reproducing the hash).

## 2. Identifying the active session (observed)

Inside a running session the CLI exports `CLAUDE_CODE_SESSION_ID=<uuid>`. The
same id names the transcript file and is the `sessionId` field on every
record. `~/.claude/sessions/<pid>.json` also maps the CLI's pid to the id and
cwd.

**Caveat (observed):** a `claude` child process spawned from inside a session
*inherits* `CLAUDE_CODE_SESSION_ID` and will write into the parent's session
id. Any tooling that shells out to `claude` from a session must unset it (the
prototype's `cc.sh` wrapper does).

## 3. What constitutes the resumable session (observed)

The transcript `<session-id>.jsonl` alone. Record types seen:

- `user`, `assistant`: the conversation, including `tool_use` blocks and
  `tool_result` blocks with a `toolUseResult` field carrying the raw result.
- `attachment`: harness-injected context (skill listings, deferred tools,
  token reminders). Included in the file; the harness re-derives what it
  needs on resume.
- `queue-operation`, `last-prompt`, `atis-latch`, `mode`: bookkeeping.

Records form a chain via `uuid` / `parentUuid`. Compaction summaries are
written into the same file as a `user` record flagged `isCompactSummary`
(**hypothesis**: read from the binary; no compaction was triggered here).

The sidecar dir is not needed to resume the parent conversation (observed);
it is needed only if something later wants the subagent's own transcript.
Claude Code's own remote-resume path materialises exactly this set, the main
file plus the sidecar subkeys, into a temp config dir (**hypothesis** from
reading the binary; consistent with the tests).

## 4. Environment-specific content in the transcript (observed)

- `cwd` on every record: the absolute working directory at the time.
- `gitBranch`, `version` (CLI version), `entrypoint` (`cli`, `remote_desktop`, `sdk`) on every record.
- Absolute paths inside tool inputs and results, as ordinary conversation content.
- No machine id, account id, hostname, or credentials.

None of this blocks resume. After a resume elsewhere, new records carry the
new `cwd`; the file then honestly records both (observed after the round
trip). The model sees the old paths only as conversation history and reasons
about them correctly (it noted on its own that the file it had read lived in
the source directory).

## 5. What `claude --resume <id>` requires (observed + binary reading)

Lookup order for the transcript, from the binary:

1. `<config-dir>/projects/<key-of-current-cwd>/<id>.jsonl`
2. the same under project keys of linked git worktrees
3. a scan of **every** `projects/*/` directory for `<id>.jsonl`; if the id
   appears under more than one key the scan returns nothing and resume fails
   with `No conversation found with session ID`.

Then it needs working credentials in the destination (the usual login or
API key). It does not need `.claude.json`, a `sessions/` entry, the original
cwd to exist, the repo contents, or the trust dialog to have been accepted for
the directory (observed in `-p` mode; **hypothesis** that interactive mode
may still show the per-directory trust prompt on first use).

## 6. Minimal capsule (derived from the above)

```
ClaudeSessionCapsule
  capsule_version              "0.1"
  source_harness               {name: "claude-code", version: "<from records>"}
  session_id                   the uuid; the only thing resume actually keys on
  project_key                  original projects/ subdir name (lets an importer keep it verbatim)
  original_working_directory   last cwd seen in the records (informational; enables a path map later)
  git_branch                   informational
  exported_at
  stats                        {records, user_messages, assistant_messages}
  files[]
    relative_destination       "<id>.jsonl" or "<id>/subagents/..."; relative to a projects/<key>/ dir
    bytes, sha256, base64
```

Only `session_id` and `files[0]` are strictly required. Everything else is
there so an importer can choose a destination key and a human can see what
they are receiving. Native files are carried byte-for-byte.

Import rule that matters: install the transcript under **exactly one**
project key on the destination. Preferred: the key of the directory the
recipient will resume from (hits lookup step 1, immune to duplicates
elsewhere). Fallback: the original key (relies on step 3, so the id must not
already exist under another key).

Path handling: no translation is required for resume. A
`source_repo_root -> destination_repo_root` map is still worth carrying as
metadata so the receiving agent knows where the old absolute paths now live;
it should be applied by the agent's understanding, not by rewriting the
transcript.

## 7. The prototype

`claude_session_capsule.py` implements export / inspect / import with no
dependencies beyond the standard library.

```
# in the source session (uses $CLAUDE_CODE_SESSION_ID and $CLAUDE_CONFIG_DIR|~/.claude)
python3 claude_session_capsule.py export -o capsule.json

# anywhere
python3 claude_session_capsule.py inspect capsule.json

# on the destination; --cwd picks the project key the recipient will resume from
python3 claude_session_capsule.py import capsule.json --cwd /workspace/switchboard
cd /workspace/switchboard && claude --resume <session-id>
```

Import refuses when the id already exists under a different project key
(would make resume ambiguous) unless `--force`, and moves any file it would
overwrite to `<name>.bak-<timestamp>` first.

Payload sizes seen: a two-turn session ~30 KB; the live investigation session
537 KB after ~40 tool calls. Base64 adds a third. Switchboard's messages and
blackboard take arbitrary JSON bodies (no size cap found in `server.py` or
`config.py`), and the blackboard is already documented as the place for
"handoffs too big for a message", so a capsule fits the existing transport
without a blob layer for now.

## 8. Experiment log (commands)

All state under an isolated scratch dir; `cc.sh` is a wrapper that sets
`CLAUDE_CONFIG_DIR`, `cd`s, and unsets `CLAUDE_CODE_SESSION_ID`,
`CLAUDECODE`, `CLAUDE_CODE_REMOTE_SESSION_ID` so the child is its own session.

```
# Environment A: new session, fixed id, a codeword and a tool call
cc.sh $S/envA $S/work/projA -p --model haiku --session-id $SID --allowedTools Read \
  "memorize codeword TANGERINE-4471; read notes.txt and name the animal"
#  -> envA/projects/<key-of-projA>/$SID.jsonl

# baseline: resume at the source
cc.sh $S/envA $S/work/projA -p --resume $SID "codeword and animal?"      # TANGERINE-4471, pangolin

# Environment B1: empty config dir, file under ORIGINAL key, different cwd
mkdir -p envB1/projects/<key-of-projA>; cp ...$SID.jsonl there
cc.sh $S/envB1 $S/work/projB -p --resume $SID "..."                       # correct

# Environment B2: empty config dir, file under DESTINATION key
mkdir -p envB2/projects/<key-of-projB>; cp ...
cc.sh $S/envB2 $S/work/projB -p --resume $SID "..."                       # correct

# B2 adds VIOLET-9020; copy B2's file back over A's (after backing up); resume in A
#   -> lists TANGERINE-4471 then VIOLET-9020

# ambiguity: same id under both keys, cwd = projC
#   -> "No conversation found with session ID"

# subagent session in envA2 -> sidecar dir appears; resume in B4 with main file only -> works

# script end to end: export envA2 -> import envB5 --cwd projE -> resume: works
#                    import envB6 (original key) -> resume from projF: works
#                    duplicate guard refuses; re-import backs up

# the live session: export from ~/.claude -> import envB7 --cwd projG -> resume: correct
```

## 9. Open questions before this becomes a Switchboard feature

- **Version skew** (untested, single version available here): the records
  carry `version`; the importer should at least warn when major/minor differ.
- **Interactive resume** (untested): all tests used `-p`. The binary uses the
  same loader for both, so this is expected to work, and the per-directory
  trust prompt is the likely only difference.
- **Concurrent continuation**: nothing stops both sides resuming the same id.
  Claude Code refuses to resume a session another live process holds
  (`sessions/<pid>.json`), but only on the same machine. Switchboard's leases
  are the obvious fit: hand off = release here, acquire there.
- **Secrets in transcripts**: tool results can contain anything the agent
  read. A capsule is as sensitive as the session; keep it in an encrypted
  room and short-lived on the blackboard.
- **What the transcript does not carry**: repo/filesystem state, environment
  variables, running processes, MCP server state, background tasks. Those are
  the separate "repo portability" and "runtime portability" concerns.

## 10. Suggested Switchboard surface (unchanged from the brief)

```
export_session()            -> capsule           (this prototype's export)
send_session(agent, capsule)                     blackboard key + DM pointer
import_session(capsule)     -> resume command    (this prototype's import)
resume_session(session_id)                       exec claude --resume
handoff_session(agent)                           export + lease release + send + notify
```

Cross-harness ("normalized") handoff stays a separate, lossy problem; the
capsule's `source_harness` field is the hook for it.
