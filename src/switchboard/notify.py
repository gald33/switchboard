"""Wake-on-write notification for long-polling readers.

Without this a waiting agent re-queries the database several times a second to
ask a question whose answer is almost always "nothing". Measured at 4.1
reads/sec per *idle* agent, scaling linearly — 1,000 connected agents cost
~4,100 queries/sec of pure noise. That number, not message volume, is what
sets the cost of an idle agent on a shared hub.

A waiter registers its interest, then drains, then sleeps until a writer wakes
it. Registering *before* draining is what makes it correct: a write landing in
the gap resolves the waiter's future, so the sleep returns immediately and the
waiter re-drains rather than missing the message.

**This is an optimization, never a correctness dependency.** The notifier is
in-process, so it cannot see writes made by another worker or another hub
instance sharing the same database. Waiters therefore also re-check on a slow
floor (`fallback_interval`), which is what actually guarantees delivery. Get
this backwards and a second uvicorn worker silently breaks message delivery in
a way no single-process test can reproduce.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class Notifier:
    """Fan-out from channel writes to the requests waiting on them."""

    def __init__(self) -> None:
        self._waiters: dict[tuple[str, str], set[asyncio.Future[None]]] = defaultdict(set)

    def register(self, workspace: str, channels: list[str]) -> asyncio.Future[None]:
        """Claim interest in ``channels``. Call this BEFORE reading."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        for channel in channels:
            self._waiters[(workspace, channel)].add(future)
        return future

    def unregister(self, workspace: str, channels: list[str],
                   future: asyncio.Future[None]) -> None:
        for channel in channels:
            key = (workspace, channel)
            bucket = self._waiters.get(key)
            if bucket is None:
                continue
            bucket.discard(future)
            if not bucket:
                # Channels are unbounded user-supplied strings; an empty set
                # left behind is a slow leak on a long-lived hub.
                self._waiters.pop(key, None)

    def notify(self, workspace: str, channel: str) -> None:
        """Wake everything waiting on this channel. Safe to call always."""
        bucket = self._waiters.get((workspace, channel))
        if not bucket:
            return
        for future in tuple(bucket):
            if not future.done():
                future.set_result(None)

    @property
    def waiting(self) -> int:
        """Distinct waiters currently registered — exposed for /stats."""
        return len({id(f) for bucket in self._waiters.values() for f in bucket})
