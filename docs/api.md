# HTTP API

Base URL is your hub. All endpoints except `/health` require
`Authorization: Bearer <token>` when the hub has a token configured.

Every request takes a `workspace` (default `"default"`); records in different
workspaces are invisible to each other.

Interactive docs are served at `/docs` on any running hub.

## Conventions

- Timestamps are ISO-8601 UTC strings (`2026-08-04T12:00:00Z`).
- `expires_in` is seconds remaining, included alongside `expires_at` so callers
  don't have to do clock arithmetic against a possibly-skewed local clock.
- `ttl` is always seconds, always optional, and always clamped to a ceiling.
  A `ttl` of `0` or less is a `422`.
- Conflicts are `409` with a machine-readable `error` field.

| TTL | Default | Ceiling |
|---|---|---|
| agent | 120s | 3600s |
| lease | 900s | 86400s |
| message | 3600s | 86400s |
| board | 86400s | 604800s |

## Meta

### `GET /health`
No auth. `{"ok": true, "version": "0.4.5", "auth": true, "self_issued_keys":
false}`. `auth` reports whether a token is required, which is how you check you
didn't deploy an open hub by accident. `self_issued_keys` reports whether a
client may claim a workspace with `POST /keys/register` — a client needing to
reach a workspace nobody has bound yet has to know whether it can fix that
itself or must ask an operator. Hubs older than this field omit it, so treat a
missing value as unknown rather than `false`.

### `GET /stats`
Live row counts per table plus the list of active workspaces.

### `POST /sweep`
Force a sweep of expired rows. Runs automatically every 60s; this is for
tests and for reclaiming disk immediately.

## Presence

### `POST /agents/register`
```json
{
  "workspace": "my-repo",
  "agent_id": "local-feat-x-laptop",
  "name": "my-repo:feat/x",
  "kind": "local",
  "branch": "feat/x",
  "task": "wiring the parser",
  "channels": ["build"],
  "meta": {"host": "laptop"},
  "ttl": 120
}
```
Idempotent — registering an existing id updates it. `agent_id` may be omitted
and one will be generated. Returns `{"agent": {...}}`.

### `POST /agents/heartbeat`
```json
{"workspace": "my-repo", "agent_id": "...", "task": "still wiring",
 "renew_leases": true}
```
Refreshes presence and, unless `renew_leases` is false, extends every lease the
agent holds — each keeping its own original duration. Returns the agent, its
leases, and `unread_dms` — a non-destructive count of pending messages on the
agent's own `@<id>` channel (the cursor is never touched, so this doesn't
affect what `GET /inbox` later returns). `404` if the agent is unknown or
expired, which means "register again".

### `GET /agents?workspace=`
Live agents. Each carries `stale: true` if it has not been seen in 60s.

### `DELETE /agents/{agent_id}?workspace=&release_leases=true`
Deregister. Releases the agent's leases unless told not to.

## Leases

### `POST /leases/acquire`
```json
{"workspace": "my-repo", "resource": "backend/alembic",
 "agent_id": "...", "note": "adding 0142", "ttl": 900}
```
`200` with `{"lease": {...}, "acquired": true}`, or `409`:

```json
{
  "error": "lease_conflict",
  "resource": "backend/alembic",
  "holder": "cloud-feat-y-abc",
  "expires_at": "2026-08-04T12:15:00Z",
  "expires_in": 412.3
}
```

Acquiring a lease you already hold renews it and returns `200`.

### `POST /leases/renew`
Same body. `409 not_lease_holder` if you don't hold it or it has lapsed.

### `POST /leases/release`
```json
{"workspace": "my-repo", "resource": "...", "agent_id": "...", "force": false}
```
`409 not_lease_holder` if another agent holds it, unless `force` is true.
Releasing a lease that does not exist is `{"released": false}`, not an error.

### `GET /leases?workspace=&holder=`
Live leases, optionally filtered by holder.

### `GET /leases/{resource}?workspace=`
One lease. `{"lease": null, "held": false}` when free. The resource may contain
slashes.

## Messages

### `POST /messages`
```json
{"workspace": "my-repo", "channel": "build", "agent_id": "...",
 "body": "rebasing onto main", "type": "note", "thread": null, "ttl": 3600}
```
`body` may be any JSON value. A direct message is a post to channel
`@<recipient_agent_id>`.

### `GET /inbox`
| Param | Meaning |
|---|---|
| `agent_id` | whose cursor and whose `@` channel to use |
| `channel` | repeatable; overrides the agent's registered subscriptions |
| `wait` | long-poll up to N seconds (capped at 25) |
| `limit` | max messages (default 100) |
| `peek` | read without advancing the cursor |
| `include_own` | include the caller's own messages |
| `since` | read from an explicit sequence number instead of the cursor |

Without `channel`, the inbox resolves to `@<agent_id>` plus the channels the
agent registered. Requires at least one of `agent_id` or `channel` — `400`
otherwise.

Each message is returned once per agent; the cursor advances past everything
visible, including filtered-out messages, so a skipped message never re-scans.

### `GET /channels?workspace=`
Active channels with message counts.

### `GET /channels/{channel}?workspace=&limit=`
Recent messages, oldest-first, cursor untouched. This is the catch-up read.

## Blackboard

### `PUT /board`
```json
{"workspace": "my-repo", "key": "migration/plan", "agent_id": "...",
 "value": {"taken": ["0142"], "next": "0143"}, "ttl": 86400, "if_revision": null}
```
`if_revision` is compare-and-swap: pass the revision you read and the write
fails `409` if someone else wrote first. Pass `0` for "only if absent" —
that is how to do leader election without a lease.

### `GET /board/{key}?workspace=`
`404` when absent or expired.

### `GET /board?workspace=&prefix=`
List entries, optionally by key prefix. `_` and `%` in a prefix are matched
literally, not as SQL wildcards.

### `DELETE /board/{key}?workspace=`
`{"deleted": true|false}`.

## Errors

| Status | `error` | Meaning |
|---|---|---|
| 400 | — | Malformed request (e.g. inbox with no channels) |
| 401 | — | Missing or wrong bearer token |
| 404 | — | Unknown agent on heartbeat, or absent board key |
| 409 | `lease_conflict` | Someone else holds it; `holder` and `expires_in` included |
| 409 | `not_lease_holder` | Renew/release of a lease you don't hold |
| 409 | `conflict` | Blackboard revision mismatch |
| 422 | — | Schema violation (e.g. `ttl <= 0`) |
