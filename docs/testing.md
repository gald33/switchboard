# Testing against a hub

If you are writing an agent that coordinates through Switchboard, you need a
hub in your tests. `switchboard.testing` gives you one: the real FastAPI app
over a real SQLite store, in your test process, with a clock you control.

```python
from switchboard.testing import hub

def test_two_agents_cannot_both_take_the_migration():
    with hub() as h:
        a, b = h.client("a"), h.client("b")
        a.acquire("migrations/0007")
        with pytest.raises(LeaseHeld):
            b.acquire("migrations/0007")
```

Requires the server extra: `pip install 'agent-switchboard[server]'`.

As a fixture, which is how most suites will want it:

```python
@pytest.fixture
def h():
    with hub() as handle:
        yield handle
```

## Why not mock the client

Because the thing you are testing is coordination, and coordination is the
behaviour of a *shared* thing under concurrent access. A mocked `Client`
returns whatever you told it to, so a test built on one asserts that your code
handles the answers you already imagined. The two failures that actually
happen — a lease you thought you held having expired, and two agents racing
for the same key — are exactly the ones a mock cannot produce.

The hub here is not a stand-in. Requests go through the same routing,
validation, encryption boundary, perimeter check and SQL that they go through
in production. The only difference is the transport: an in-process ASGI
transport rather than a socket.

## Controlling time

Everything in Switchboard expires, which means most of what is worth testing
happens minutes into the future. `h.advance(seconds)` moves the hub's clock:

```python
def test_a_lease_outlives_a_crashed_holder(h):
    worker = h.client("worker")
    worker.acquire("db/migrations", ttl=900)

    # the worker dies here — no release, no renewal
    h.advance(901)

    assert h.client("next").acquire("db/migrations")["holder"] == "next"
```

The clock is per hub and injected into the app (`create_app(clock=...)`),
so nothing is patched globally and two hubs in one test keep two independent
nows. It starts at a fixed epoch rather than "now", so a test that asserts on
a timestamp reads the same on every machine.

Expiry is evaluated at read time, so `advance` alone is enough to make
something disappear. `h.sweep()` is for when the row deletion itself is what
you are testing.

What it does not do is fake `time.sleep` or the event loop. A long poll's
`wait=` deadline still runs on the real clock — that is the transport holding
a connection open, not hub state ageing.

## Asking what the hub thinks is true

Two kinds of question, and they want different tools.

**"What would a peer see?"** — ask a client. This is the honest one: it is the
same call another agent would make, and it goes over HTTP.

```python
assert [a["agent_id"] for a in h.client("observer").agents()] == ["worker"]
```

**"What is actually recorded?"** — read `h.store`, or the shortcuts on the
handle (`h.agents()`, `h.leases()`, `h.messages(channel)`, `h.board()`). These
go straight to SQLite, so they do not disturb anything — reading an inbox
through a client commits a read cursor, and these do not.

The distinction matters most under encryption, where the two answers differ on
purpose: the client sees plaintext, the store holds ciphertext, and the gap
between them is the feature.

> If you call `h.store` methods directly rather than through the shortcuts,
> pass `now=h.now`. Every store method falls back to `time.time()` when not
> told otherwise, and this hub's now is not the wall clock — so a bare
> `store.list_agents(workspace=ws)` compares a fixed-epoch expiry against
> today and reports an empty roster.

## Encryption and the perimeter

```python
with hub(key=generate_key()) as h:      # every client gets the key
    a, b = h.client("a"), h.client("b")  # ...so they can read each other
    stranger = h.client("z", key=generate_key())   # and this one cannot

with hub(token="s3cret") as h:
    h.client("a")            # carries the token
    h.raw(token=None)        # the request that should be refused
```

`hub()` defaults to an in-memory database. Pass `db=str(tmp_path / "hub.db")`
when the bytes on disk are the subject — asserting that a hub operator reading
the file learns nothing, for instance.

## Testing code that builds its own client

Some code constructs a `Client` internally and gives you no way to pass one in
— the Switchboard CLI does exactly this. `h.client_class()` returns a drop-in
bound to the hub:

```python
monkeypatch.setattr(switchboard.cli, "Client", h.client_class())
assert main(["--url", h.url, "-w", h.workspace, "announce"]) == 0
```

Everything the caller chose — workspace, agent id, key — is honoured; only the
transport is replaced. If your code takes a `ClientConfig` instead, `h.client_config()`
gives you one pointed at the hub.

## Async

`h.async_client()` returns an `AsyncClient` over an ASGI transport onto the
same hub, so sync and async clients in one test see the same state. It is not
registered for you, because registering is a coroutine.

## The handle

| | |
|---|---|
| `h.client(name, *, register=False, key=..., workspace=...)` | a `Client`; each call is a distinct agent |
| `h.async_client(name)` | an `AsyncClient` onto the same hub |
| `h.client_class()` / `h.client_config()` | for code that builds its own client |
| `h.advance(seconds)` / `h.now` / `h.sweep()` | the clock |
| `h.agents()` / `h.leases()` / `h.messages(ch)` / `h.board()` | state, read without disturbing it |
| `h.store` / `h.app` | the real store and the real app |
| `h.http` / `h.raw(token=...)` | raw HTTP, for asserting on the wire format |
| `h.url` / `h.workspace` / `h.token` / `h.key` | what the clients were configured with |

## Drills are the other kind of test

`switchboard.testing` is for testing your code against a hub. A
[drill](drills.md) is for testing whether *agents* coordinate — real
`claude -p` sessions given one task, observed only through the hub. Use a
drill when the question is about the protocol working in practice, and this
module when the question is about your code.
