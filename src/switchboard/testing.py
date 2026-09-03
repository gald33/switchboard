"""A real hub, in your test process, with a clock you control.

The client in ``client.py`` is the SDK for talking to a hub. This is the other
half: something to point it at. A hub here is the actual FastAPI app over the
actual SQLite store, reached through an in-process transport — not a fake, not
a mock, not a subprocess. Requests take the same code path they take in
production, and assertions can be made either through the client (what a peer
agent would see) or against ``hub.store`` (what is really recorded).

    from switchboard.testing import hub

    def test_two_agents_cannot_both_take_the_migration():
        with hub() as h:
            a, b = h.client("a"), h.client("b")
            a.acquire("migrations/0007")
            with pytest.raises(LeaseHeld):
                b.acquire("migrations/0007")

As a pytest fixture, which is how most suites will want it::

    @pytest.fixture
    def h():
        with hub() as handle:
            yield handle

Why this ships in the package rather than living in ``tests/``: anyone writing
an agent against the SDK needs exactly this to test their agent, and the
alternative is that they each reinvent it — or worse, mock the client and test
their mock. It carries no import cost for client-only installs, because
nothing imports it unless you ask for it. It does need the ``server`` extra.

**Time.** Every hub gets a :class:`Clock` starting at a fixed epoch, and
``h.advance(seconds)`` moves it. This is the part that is otherwise
impossible: the whole model is TTLs, so most of what is worth testing —
a lease outliving its holder, presence going stale, a board entry expiring —
happens minutes out, and a suite cannot wait. The clock is per hub and is
threaded through the app explicitly (see ``create_app``), so nothing is
patched globally and two hubs in one process do not share a now.

What it deliberately does not do is fake ``time.sleep`` or the event loop.
Long-poll deadlines in ``inbox(wait=)`` still run on the real clock, because
those are about the transport holding a connection open rather than about
when hub state expires, and pretending otherwise would test nothing.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any, Iterator

from .client import AsyncClient, Client
from .config import ClientConfig, ServerConfig
from .store import Store

__all__ = ["Clock", "Hub", "hub", "EPOCH", "BASE_URL"]

#: A fixed point in time for every hub to start from, rather than "now". Tests
#: that print or assert on a timestamp then read the same on every machine and
#: in every year, and a test that accidentally depends on the wall clock fails
#: everywhere instead of only in December.
EPOCH = 1_700_000_000.0

#: The URL clients are configured with. Nothing resolves it — the transport is
#: in-process — but it has to be *something*, and it must not be a loopback
#: address: `isolation_warning` warns about those, so using one would print a
#: spurious warning in every test that goes through the CLI.
BASE_URL = "http://hub.test"

#: Long-polls hold a connection for up to 25s, and httpx's default read timeout
#: is well under that, so a test exercising `inbox(wait=)` would fail on the
#: client side before the hub ever answered.
TIMEOUT = 60.0

#: "the hub's own token", as distinct from ``None``, which means "send no
#: token at all" — the request a closed perimeter is supposed to refuse.
_KEEP: Any = object()


class Clock:
    """A settable source of "now", in epoch seconds.

    Callable, so it can be handed straight to ``create_app(clock=...)``.
    """

    def __init__(self, start: float = EPOCH) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        """Move time forward. Returns the new now."""
        if seconds < 0:
            raise ValueError("time only moves forward; a hub cannot un-expire a lease")
        self.now += float(seconds)
        return self.now


class Hub:
    """A running hub and the things you need to talk to it.

    Built by :func:`hub`; not usually constructed directly.
    """

    def __init__(self, *, app: Any, store: Store, clock: Clock, http: Any,
                 config: ServerConfig, workspace: str, token: str | None,
                 key: str | None, peer_log: str = "",
                 write_key: str | None = None) -> None:
        #: The FastAPI application, for anything that wants the app object.
        self.app = app
        #: The store behind it. Read it to assert on what was *recorded*, as
        #: opposed to what a client can see — the two differ under encryption,
        #: which is the point of most crypto tests.
        self.store = store
        self.clock = clock
        #: An authenticated raw HTTP client, for asserting on the wire format
        #: itself: status codes, error payloads, field names.
        self.http = http
        self.config = config
        self.workspace = workspace
        self.token = token
        #: The workspace encryption key, if this hub was built with one. Every
        #: client it hands out uses it by default, so they can read each other.
        self.key = key
        #: The room's write key, if this hub was built with one. Then the
        #: workspace is a write-protected ``ws_…`` room named by that key, every
        #: client it hands out signs with it by default, and ``write_key=""``
        #: on :meth:`client` makes a reader — present, reading, refused on
        #: every write.
        self.write_key = write_key
        #: Where clients of this hub keep their peer-key witness log. Empty by
        #: default so tests do not write to the real one in ``~/.switchboard``
        #: or inherit each other's witnessing; pass a tmp path to exercise it.
        self.peer_log = peer_log
        self.url = BASE_URL
        self._clients: list[Client] = []

    # --- time ---------------------------------------------------------------

    @property
    def now(self) -> float:
        return self.clock.now

    def advance(self, seconds: float) -> float:
        """Move this hub's clock forward.

        Expiry is evaluated at read time, so this is enough on its own to make
        a lease, an agent or a board entry expire — no sweep required. Call
        :meth:`sweep` only when the deletion itself is what is under test.
        """
        return self.clock.advance(seconds)

    def sweep(self) -> dict[str, int]:
        """Hard-delete everything expired as of now. Returns per-table counts."""
        return self.store.sweep(now=self.clock.now)

    # --- clients ------------------------------------------------------------

    def client_config(self, *, workspace: str | None = None, agent_id: str | None = None,
                      key: str | None = None, write_key: str | None = None) -> ClientConfig:
        """The config a client of this hub needs.

        Useful on its own for testing code that takes a ``ClientConfig`` and
        builds its own client — the MCP bridge, for instance.
        """
        return ClientConfig(
            url=self.url,
            url_source="explicit",
            token=self.token,
            workspace=workspace or self.workspace,
            agent_id=agent_id,
            key=self.key if key is None else key,
            write_key=(self.write_key if write_key is None else write_key) or None,
            # Off by default in tests. The peer-key log is per *machine* and
            # deliberately outlives a process, so a shared one would carry
            # witnessing between test cases and make a swap assertion depend on
            # what ran before it. Tests that want it point it at a tmp path.
            peer_log=self.peer_log,
        )

    def client(self, name: str | None = None, *, agent_id: str | None = None,
               workspace: str | None = None, key: str | None = None,
               write_key: str | None = None,
               register: bool = False, kind: str = "test",
               **register_kwargs: Any) -> Client:
        """A :class:`~switchboard.Client` wired to this hub.

        ``name`` doubles as the agent id unless one is given, because a test
        that reads ``holder == "alice"`` is a test you can debug. Pass
        ``register=True`` for the common case where the agent needs to be
        present before the assertion means anything — presence, DM routing and
        roster checks all need it; leases and the board do not.

        Each call is a distinct agent, exactly as two clients in one process
        are two agents in production.

        ``key`` overrides the hub's: another key makes this client a stranger
        who cannot read the others, and ``""`` makes it an unencrypted client
        in an encrypted workspace — which is a real misconfiguration and worth
        testing, because nothing raises when it happens.

        ``write_key`` overrides the hub's the same way, and ``""`` makes a
        *reader*: a client with no write key in a write-protected room, which
        the hub admits to every GET and refuses on every write.
        """
        name = name or f"agent-{uuid.uuid4().hex[:8]}"
        config = self.client_config(workspace=workspace, agent_id=agent_id or name, key=key,
                                    write_key=write_key)
        client = Client(
            config,
            agent_id=agent_id or name,
            key=config.key,
            http=self._transport(),
        )
        self._clients.append(client)
        if register:
            client.register(name=name, kind=kind, **register_kwargs)
        return client

    def async_client(self, name: str | None = None, *, agent_id: str | None = None,
                     workspace: str | None = None, key: str | None = None,
                     write_key: str | None = None) -> AsyncClient:
        """An :class:`~switchboard.AsyncClient` wired to this hub.

        Not registered for you: registering is a coroutine, and a fixture that
        silently awaited one would need an event loop it does not own.
        """
        import httpx

        name = name or f"agent-{uuid.uuid4().hex[:8]}"
        config = self.client_config(workspace=workspace, agent_id=agent_id or name, key=key,
                                    write_key=write_key)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=self.url,
            headers=_auth_headers(self.token),
            timeout=TIMEOUT,
        )
        return AsyncClient(config, agent_id=agent_id or name, key=config.key, http=http)

    def client_class(self) -> type[Client]:
        """A ``Client`` drop-in bound to this hub, for code that builds its own.

        The CLI is the case that needs it: it constructs a ``Client`` from
        parsed arguments deep inside ``main()``, so a test driving the CLI
        in-process has no client to hand it — only a name to replace::

            monkeypatch.setattr(switchboard.cli, "Client", h.client_class())

        Everything the caller chose — workspace, agent id, key — is honoured;
        only the transport is ours.
        """
        outer = self

        class BoundClient(Client):
            def __init__(self, config: ClientConfig | None = None, *,
                         agent_id: str | None = None, timeout: float = TIMEOUT,
                         key: str | None = None,
                         http: Any = None) -> None:
                super().__init__(
                    config, agent_id=agent_id, key=key,
                    http=http if http is not None else outer._transport(),
                )

        return BoundClient

    def raw(self, *, token: str | None = None) -> Any:
        """A raw HTTP client, with the token you name rather than the hub's.

        For testing the perimeter itself — the case where the interesting
        request is the one that should be turned away.
        """
        return self._transport(token)

    def _transport(self, token: str | None = _KEEP) -> Any:
        """An HTTP client onto this app, sharing the hub's event loop.

        The sharing is the whole subtlety. A ``TestClient`` that has not been
        entered as a context manager runs each request in a portal of its own,
        so two of them are two event loops — and then a message posted through
        one cannot wake a long poll parked in the other, because the future it
        resolves belongs to a loop nobody is running. The symptom is a
        ``inbox(wait=)`` that always runs to its poll floor instead of waking
        on arrival: not a hang, just every notification test quietly measuring
        the fallback. Entering one per client is not the fix either — that
        would run the app's lifespan per client, so N sweepers, and the first
        client to close would close the store out from under everyone.

        So: one entered client owns the loop, and everything else borrows its
        transport, which carries the portal with it.
        """
        import httpx

        token = self.token if token is _KEEP else token
        transport = getattr(self.http, "_transport", None)
        if transport is None:  # pragma: no cover — httpx moved its internals
            from starlette.testclient import TestClient

            client = TestClient(self.app, base_url=self.url,
                                headers=_auth_headers(token))
            client.timeout = TIMEOUT
            return client
        return httpx.Client(
            transport=transport, base_url=self.url,
            headers=_auth_headers(token), timeout=TIMEOUT,
        )

    # --- oracles ------------------------------------------------------------
    #
    # Convenience readers for the "what does the hub think is true right now"
    # question, answered at the store rather than over HTTP so that asserting
    # on state never disturbs it. Reading an inbox through a client commits a
    # read cursor; these do not.
    #
    # Use these rather than calling the store yourself, because every store
    # method falls back to `time.time()` when not told otherwise — and this
    # hub's now is not the wall clock. A raw `store.list_agents(workspace=ws)`
    # compares an epoch-2023 expiry against today and reports an empty roster,
    # which looks exactly like a bug in the code under test. If you do go to
    # the store directly, pass `now=hub.now`.

    def agents(self, workspace: str | None = None) -> list[Any]:
        return self.store.list_agents(
            workspace=workspace or self.workspace, now=self.clock.now
        )

    def leases(self, workspace: str | None = None) -> list[Any]:
        return self.store.list_leases(
            workspace=workspace or self.workspace, now=self.clock.now
        )

    def messages(self, channel: str, *, workspace: str | None = None,
                 limit: int = 100) -> list[Any]:
        return self.store.peek(
            workspace=workspace or self.workspace, channel=channel,
            limit=limit, now=self.clock.now,
        )

    def board(self, *, prefix: str | None = None,
              workspace: str | None = None) -> list[Any]:
        return self.store.board_list(
            workspace=workspace or self.workspace, prefix=prefix, now=self.clock.now,
        )

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        for client in self._clients:
            with contextlib.suppress(Exception):
                client._http.close()
        self._clients.clear()


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@contextlib.contextmanager
def hub(*, workspace: str | None = None, token: str | None = None,
        key: str | None = None, db: str | None = None, start: float = EPOCH,
        store: Store | None = None, server_config: ServerConfig | None = None,
        peer_log: str = "", write_key: str | None = None,
        **config_kwargs: Any) -> Iterator[Hub]:
    """Run a hub for the duration of the block.

    ``db`` defaults to an in-memory database, which is both faster and safer
    than a temp file — there is no path for a later test to inherit. Pass one
    (``tmp_path / "hub.db"``) when the bytes on disk are the subject, as in the
    encryption tests.

    ``key`` makes this an encrypted workspace: every client gets that key, so
    they can read each other while ``hub.store`` shows only ciphertext, which
    is the setup for asserting that the hub cannot read what it is holding.

    ``token`` closes the perimeter. Clients get it automatically; use
    ``hub.raw(token=...)`` to make the request that should be refused.

    ``write_key`` makes the workspace a write-protected room — the ``ws_…``
    identifier the key names, unless ``workspace`` is also given, which is
    only sensible when the two agree. Every client signs with it by default;
    ``hub.client(write_key="")`` is the reader whose writes should be refused.
    Without one, ``workspace`` defaults to ``"test-workspace"``.

    ``store`` takes an already-built store, for the tests that need to
    instrument one — counting reads, say, to prove a long poll is not
    secretly polling. Anything else left to the defaults.

    Remaining keywords go to ``ServerConfig`` (``load_target_ms``,
    ``sweep_interval``), or pass a whole ``server_config``.
    """
    from starlette.testclient import TestClient

    from .server import create_app

    if workspace is None:
        if write_key:
            from .writekey import RoomWriteKey

            workspace = RoomWriteKey.from_seed(write_key).workspace
        else:
            workspace = "test-workspace"
    config = server_config or ServerConfig(
        db_path=db or (store.path if store else ":memory:"),
        token=token, **config_kwargs,
    )
    clock = Clock(start)
    # One store, shared: the app would otherwise open its own, and with an
    # in-memory database that is a second, empty hub that no assertion can see.
    store = store if store is not None else Store(config.db_path)
    app = create_app(config, store=store, clock=clock)

    handle: Hub | None = None
    with TestClient(app, base_url=BASE_URL, headers=_auth_headers(token)) as http:
        http.timeout = TIMEOUT
        handle = Hub(
            app=app, store=store, clock=clock, http=http, config=config,
            workspace=workspace, token=token, key=key, peer_log=peer_log,
            write_key=write_key,
        )
        try:
            yield handle
        finally:
            handle.close()
    # The app's lifespan closes the store on the way out; nothing to do here.
