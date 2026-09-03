# ARCS.md — what's in flight, at arc level

<!-- GENERATED FILE — DO NOT EDIT BY HAND. Regenerate with `roadmap sync`. Edit roadmap/arcs/*.yaml instead. -->

The narrative layer above `roadmap/ROADMAP.md`: *why* each theme is still open. Work items live in the roadmap graph and are listed per arc below; the prose here is the arc's own, and is the one place a multi-PR theme gets explained rather than enumerated. For when this was last regenerated ask git — `git log -1 --format=%cI -- ARCS.md` — because nothing here derives from the clock or from a graph-wide total, so two branches editing different arcs merge cleanly.

## Legend

| State | Meaning |
|---|---|
| 🟠 open | Open tail — unfinished items, or a stated unresolved decision. |
| 🔵 blocked | Nothing startable — every unfinished item is blocked, or the blocker is outside the graph and was declared. |
| 🟡 dark | Code merged, flag off **in prod env**. Declared, never derived. |
| 🟢 closed | Tail is empty and somebody said so. Declared, never derived. |

`dark`, `closed` and `blocked` may be **declared** by a human with dated evidence, because each is about something the items cannot show: an environment flag, a closure whose finished items `prune` has deleted, or a blocker outside the graph entirely. `blocked` is *also* derived when every unfinished item is itself blocked. `open` is never declared — it is the fallback every check fires on, so stating it would only silence them.

## 🟠 Open

### 🟠 Getting the thing to the people who run it

`distribution` · 1 item(s), 1 startable

`CONTRIBUTING.md` · `.github/workflows/publish.yml`

The agent side of this project is well distributed: `pip install
agent-switchboard`, a release workflow on Trusted Publishing with no stored
credentials, a version guard that refuses a mismatched tag, and a documented
ordering rule so an add-on never ships against an SDK that is not on PyPI yet.

The hub side is not. Running one from a container still means cloning the
repository and building the image yourself, which is the only place this project
asks for a source checkout in order to *use* it rather than to develop it.

One item today, and it is the only one on the board not traceable to a filed
issue — read off a line in the README rather than reported by anyone.

| item | status | priority |
|---|---|---|
| `publish-hub-container-image` | ready | later |

### 🟠 What protects a room once the hub stops authorizing anyone

`hub-boundary` · 9 item(s), 6 startable

`https://github.com/gald33/switchboard/issues/61` · `https://github.com/gald33/switchboard/issues/73` · `docs/model.md` · `docs/encryption.md`

#61 and #73 removed per-token authorization. Reaching a room is now knowing its
identifier, `hash(workspace_token)`, and encryption is the only boundary left.

That is a coherent design, and this arc is the work of actually living inside
it. Three kinds of debt fall out of a change that size, and all three are here:

* **Identifiers that leak.** The model's whole load is carried by
  unguessability, so any published identifier is a hole in it — including the
  one in this repository's own CI workflow.
* **Payloads that are not sealed.** Every field that skips `_SEAL_BODY` hands
  the hub something it claims not to hold. `meta` still carries the repo name.
* **Reasoning that outlived its subject.** Comments and docs still describe the
  resolvers that were deleted, which sends readers looking for machinery that
  was removed on purpose.

The fourth item is the policy question the removal opened rather than closed:
what replaces the abuse control that per-token limits used to provide. It is
deferred, not abandoned — the answer is written down so it is not rediscovered
under pressure.

| item | status | priority |
|---|---|---|
| `ci-workspace-is-public` | ready | now |
| `stale-resolver-references` | ready | now |
| `seal-agent-meta` | ready | next |
| `abuse-control-after-authorization` | deferred | — |
| `hub-origin-reachable-bypassing-the-edge` | ready | — |
| `provisioned-token-is-stale-and-nothing-says-so` | ready | — |
| `read-only-rooms` | done | — |
| `roles-and-authority-between-agents` | deferred | — |
| `standing-checks-that-nothing-runs` | ready | — |

### 🟠 The first ten minutes, for somebody who has not read the docs

`setup-and-first-run` · 18 item(s), 12 startable

`https://github.com/gald33/switchboard/issues/86` · `https://github.com/gald33/switchboard/issues/88` · `https://github.com/gald33/switchboard/issues/89` · `docs/environments.md`

Switchboard's characteristic failure is not an error — it is an agent that
connects, registers, and coordinates with nobody. `docs/environments.md` names
three ways that happens, and the common thread is that every one of them looks
like success from the inside.

These three items are that theme at setup time. `init` does not produce the
rooms record the authoritative doc calls the model, so the documented shape and
the generated shape differ. A connection failure prints forty lines of httpx
frames without naming the URL it tried, which is the single fact that would
reveal a silent fallback to loopback. And the one warning that does guard
against quietly-broken wiring fires on correct configurations, including this
repository's own — which is how a warning stops being read.

None of them is a hard bug. Together they decide whether a first run that went
wrong says so.

| item | status | priority |
|---|---|---|
| `connect-failure-message` | done | next |
| `cross-key-rendezvous` | ready | next |
| `init-writes-rooms-file` | ready | next |
| `known-rooms-address-book` | done | next |
| `a-lobby-derived-from-the-key` | ready | — |
| `clients-that-cannot-post` | ready | — |
| `every-silent-failure-looks-like-a-quiet-room` | ready | — |
| `identity-rebinds-on-branch-change` | ready | — |
| `joining-agent-sees-empty-inbox` | ready | — |
| `one-resolved-context-across-surfaces` | deferred | — |
| `presence-ttl-is-not-one-size` | ready | — |
| `robots-policy-for-public-hosts` | deferred | — |
| `selective-wake-for-the-listener` | ready | — |
| `stale-token-in-session-env` | ready | — |
| `timing-cold-start-in-ephemeral-environments` | deferred | — |
| `unread-dms-not-shown-outside-mcp` | ready | — |
| `write-parity-across-surfaces` | ready | — |
| `hooks-warning-false-positive` | done | later |

### 🟠 Ceilings that are honest about themselves

`ttl-ceilings` · 2 item(s), 2 startable

`https://github.com/gald33/switchboard/issues/142` · `https://github.com/gald33/switchboard/issues/85`

Everything in Switchboard expires, and the expiry is the correctness property
rather than a cleanup detail. That makes the ceilings load-bearing in two ways
that are easy to conflate, which is why these two items carry a `related_to`
edge warning against exactly that.

One is about **communication**: a ttl clamped to the ceiling comes back looking
like a ttl that was granted, so a caller plans around a number the hub never
agreed to. Whatever the ceilings are, hitting one should not resemble success.

The other is about **the values themselves**: `MAX_BOARD_TTL` is seven times
`MAX_LEASE_TTL`, which quietly falsifies the project's "everything expires
within a day" claim and forecloses a daily room epoch.

Fixing either one alone leaves the other entirely intact.

| item | status | priority |
|---|---|---|
| `ttl-clamped-silently` | ready | next |
| `board-ttl-ceiling` | ready | later |

## Items with no arc

Startable and legitimate — an item does not need an arc. Listed so the narrative layer's coverage gap is visible rather than implied.

- `intermittent-suite-failure` — Two pytest processes shared one signing socket, so a whisper opened with the wrong key
