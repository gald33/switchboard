# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
```

That is the whole toolchain. There is no build step, no code generation, and
no service to run for the tests — they spin the app up in-process.

## Layout

| File | What lives there |
|---|---|
| `store.py` | SQLite storage. All concurrency correctness is here. |
| `server.py` | FastAPI app: wire schemas, serialization, HTTP semantics. |
| `client.py` | Sync + async HTTP clients, and identity detection. |
| `cli.py` | The `switchboard` command. |
| `mcp_server.py` | MCP stdio bridge — speaks JSON-RPC directly, no SDK. |
| `config.py` | Env-driven settings and the TTL defaults/ceilings. |

The dependency direction is one-way: `store` knows nothing about HTTP,
`server` knows nothing about the CLI, and `cli`/`mcp_server` both go through
`client`. Keep it that way.

`examples/` and `extras/` are downstream of all of it, and import only what the
package exports. That is the point rather than a nicety: `extras/viewer` is a
whole application built on the published surface, and its tests
(`tests/test_example_viewer.py`, `tests/test_web_page.py`) fail if that surface
stops being enough to build it. When one of them needs a private name, export
the name — do not reach for the underscore.

The two directories differ in what they promise. `examples/` is read;
`extras/` is *installed*, as its own distribution with its own `pyproject.toml`
and version. So an add-on may only depend on `agent-switchboard` and what it
declares itself, and the `viewer-addon` CI job enforces it by installing the add-on's wheel
against the SDK **from PyPI**, somewhere that is not this repo — so an add-on
that reaches into `src/`, or uses API `main` has but no release has shipped,
fails there rather than in somebody's `pip install`. Add-ons are
named `switchboard-<thing>` and live one directory each under `extras/`.

## Things to preserve

**The client, CLI and MCP bridge must depend only on `httpx`.** An agent
should be able to join a hub without installing FastAPI. CI has a job that
fails if a server import leaks into them.

**The MCP bridge must not take an SDK dependency.** It implements the stdio
protocol directly so it cannot break when an SDK renames its API between
majors — which is exactly what happened between `mcp` 1.x and 2.0. If you add
a protocol method, add a test asserting its wire shape.

**Every record must expire.** A new table needs an `expires_at`, a read filter
on it, and an entry in `sweep()`. Correctness must not depend on the sweeper
having run — reads filter expiry themselves, and there is a test that says so.

**Read-then-write must happen inside `_tx()`.** That is `BEGIN IMMEDIATE`, and
it is the only reason two agents cannot both win the same lease. If you find
yourself reading a row and then writing based on what you read, it belongs in
one transaction.

## Testing

Tests use synthetic timestamps (`now=1000.0`) rather than sleeping, so expiry
behaviour is tested exactly and instantly. Pass `now=` explicitly to every
store call in a test that cares about time — including the assertions, since
the reads default to wall-clock.

Anything that needs a whole hub gets one from `switchboard.testing` rather
than assembling `Store` + `create_app` + `TestClient` by hand:

```python
from switchboard.testing import hub

with hub() as h:
    h.client("a").acquire("r")
    h.advance(901)          # the same synthetic-time discipline, over HTTP
```

It hands out real `Client`s, and `h.client_class()` is the supported way to
point the CLI at it (`monkeypatch.setattr(switchboard.cli, "Client", ...)`).
It is public API — [docs/testing.md](docs/testing.md) — so it is covered by
`test_testing.py`, and changing its behaviour breaks other people's suites.

`test_store.py::test_concurrent_acquire_yields_exactly_one_winner` is the one
test that must never be weakened. It runs twelve real threads through a
barrier at one resource and asserts exactly one winner. If it becomes flaky,
something is wrong with the locking, not with the test.

## Pull requests

- Run `ruff check .` and `pytest -q` before pushing.
- Add a test for anything behavioural. The suite is fast; there is no reason
  not to.
- If you change the HTTP surface, update `docs/api.md` in the same PR.
- If you add or change an MCP tool, update its description — the description
  is the entire interface an agent has to the tool, so it needs to say *when*
  to use it, not just what it does.

## Releasing

Two distributions, two tags, two workflows, and an order between them.

```bash
# the SDK
#   bump `version` in pyproject.toml, merge, then:
gh release create v0.8.0 --generate-notes

# an add-on
#   bump `version` in extras/viewer/pyproject.toml, merge, then:
gh release create viewer-v0.8.0 --generate-notes --title 'switchboard-viewer 0.8.0'
```

Each workflow ignores the other's tag prefix rather than failing on it, so a
release publishes exactly one project. Both refuse to run against anything but
a tag whose version matches the `pyproject.toml` being built — PyPI never lets
a version be reused, so a wrong publish is unfixable rather than merely
embarrassing.

**The SDK goes first.** An add-on declares a lower bound on
`agent-switchboard`, and `publish-viewer.yml` asks PyPI whether that bound is
satisfiable before it builds. Releasing the viewer against an SDK version that
is not published yet fails there, loudly, instead of shipping something
`pip install` cannot resolve.

## Design questions

Open an issue before building anything that adds a fifth primitive. Four is a
deliberate ceiling: presence, leases, messages, blackboard. Most proposals
turn out to be one of those with a different name, and the ones that aren't
are usually asking Switchboard to be a queue, a log, or a scheduler — all
things it deliberately is not. See [docs/concepts.md](docs/concepts.md).
