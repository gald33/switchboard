# Quickstart

Hub running and two agents coordinating, in about five minutes.

This walks the raw CLI so you can see each primitive on its own. If you're
setting up a repo for Claude Code agents specifically, `switchboard init`
does the wiring below for you in one command — see
[Claude Code setup](claude-code.md#the-fast-path).

## 1. Start a hub

```bash
pip install "agent-switchboard[server]"

export SWITCHBOARD_TOKEN="dev-token"
switchboard serve --port 8787
```

You should see:

```
switchboard 0.3.0 → http://127.0.0.1:8787  db=switchboard.db
```

Leave it running.

## 2. Open two more terminals

Both need the same environment:

```bash
export SWITCHBOARD_URL=http://127.0.0.1:8787
export SWITCHBOARD_TOKEN=dev-token
export SWITCHBOARD_WORKSPACE=demo
```

In the second terminal, give the agent a distinct identity so you can tell them
apart (normally this is inferred from branch and hostname):

```bash
export SWITCHBOARD_AGENT_ID=beta
```

## 3. Check in

**Terminal A:**

```bash
$ switchboard health
{ "ok": true, "version": "0.3.0", "auth": true }

$ switchboard register -c build
registered local-main-yourhost (local) in demo
```

**Terminal B:**

```bash
$ switchboard register -c build
registered beta (local) in demo

$ switchboard agents
AGENT                              KIND    BRANCH                   SEEN       TASK
beta                               local   main                     0s ago
local-main-yourhost                local   main                     31s ago
```

Both agents can now see each other.

## 4. Claim something

**Terminal A:**

```bash
$ switchboard claim db/migrations -m "adding 0142"
claimed db/migrations for 15m00s
```

**Terminal B:**

```bash
$ switchboard claim db/migrations
held by local-main-yourhost for another 14m41s
$ echo $?
2
```

Exit code 2 means conflict, which is what makes this usable in a script:

```bash
switchboard claim db/migrations -q || { echo "someone else has it"; exit 0; }
```

## 5. Talk

**Terminal A:**

```bash
$ switchboard say build "taking alembic — using 0142, next free is 0143"
posted #1 to build
```

**Terminal B:**

```bash
$ switchboard inbox
build local-main-yourhost 4s ago
  taking alembic — using 0142, next free is 0143

$ switchboard inbox
(nothing new)
```

Each message is delivered once per agent. To wait for the next one instead of
polling:

```bash
$ switchboard inbox --wait 25
```

Direct messages work the same way:

```bash
# Terminal B
$ switchboard dm local-main-yourhost "ok, I'll take 0143"
sent #2 to local-main-yourhost

# Terminal A
$ switchboard inbox
@local-main-yourhost beta 2s ago
  ok, I'll take 0143
```

## 6. Hand something off

Messages are for signals. For payloads, use the blackboard:

**Terminal A:**

```bash
$ switchboard board set migration/plan \
    '{"taken": ["0142"], "next": "0143", "files": ["orders.py"]}' --json-body
migration/plan = rev 1

$ switchboard say build "plan is on the board at migration/plan"
posted #3 to build
```

**Terminal B:**

```bash
$ switchboard board get migration/plan
{"taken": ["0142"], "next": "0143", "files": ["orders.py"]}
```

## 7. Watch it expire

The point of the whole thing. Take a short lease:

```bash
# Terminal A
$ switchboard claim scratch --ttl 10
claimed scratch for 10s

$ switchboard claims
RESOURCE     HOLDER                 EXPIRES  NOTE
db/migrations local-main-yourhost   14m02s   adding 0142
scratch      local-main-yourhost    8s

# wait ten seconds
$ switchboard claims
RESOURCE     HOLDER                 EXPIRES  NOTE
db/migrations local-main-yourhost   13m48s   adding 0142
```

`scratch` released itself. Nobody ran a cleanup, and no record of it survives.
Had terminal A been killed instead, `db/migrations` would have gone the same
way within fifteen minutes.

## 8. Release and finish

```bash
# Terminal A
$ switchboard release db/migrations
released db/migrations
```

## Next

- [Concepts](concepts.md) — the model in full, and what Switchboard is not
- [Claude Code setup](claude-code.md) — MCP tools and hooks
- [Deployment](deployment.md) — running a hub your whole team can reach
