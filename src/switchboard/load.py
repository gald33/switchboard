"""How busy the hub actually is.

The abuse design (#72) turns on one measured quantity rather than an invented
one. A capacity in requests-per-second is guesswork that changes with hardware
and with whatever else the box is doing; a *load target* is closed-loop —
admit freely below it, let effort order the queue above it — so the clearing
price and the capacity both discover themselves.

Nothing here sheds traffic. It measures, so a target can be chosen from
numbers rather than from a guess, which is the order the design asks for. The
scheduler that acts on it comes later and reads these.

Two things this gets right that a naive version does not:

**Parked long-polls are not load.** ``GET /inbox?wait=`` holds a connection for
up to 25 seconds waiting for a message, and that is the *normal* state of an
idle agent. Measuring wall-clock per request would show inbox consuming the
entire hub while doing nothing, and any scheduler reading that would throttle
everything else in favour of idleness. So a request releases its slot while
parked and takes one again to deliver.

**Queueing delay, not CPU.** CPU is noisy and lags, so a reading is late by the
time it is alarming. How long a request waits *before* being served is what
saturation actually means, moves immediately, and is independent of what the
work is — it is also the number a human can reason about, because it is what
users feel.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

#: How many recent samples the delay estimate is drawn from. Enough to be
#: stable, short enough to notice a hub going under within seconds.
_WINDOW = 256


@dataclass
class Snapshot:
    """What a scheduler, or an operator, would look at."""

    active: int
    parked: int
    peak_active: int
    #: Median and worst-case queueing delay over the recent window, in
    #: milliseconds. The p95 is the one worth targeting; the median moves
    #: too little under the load that matters.
    delay_p50_ms: float
    delay_p95_ms: float
    samples: int


class LoadMeter:
    """Active concurrency and queueing delay, cheap enough to run always.

    Thread-safe because handlers run in a threadpool: the counters are touched
    on every request, so a lock here is the difference between a number and a
    guess.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._parked = 0
        self._peak = 0
        self._delays: deque[float] = deque(maxlen=_WINDOW)

    @contextmanager
    def serving(self):
        """Hold a slot for the duration of actual work."""
        start = time.monotonic()
        self._enter()
        try:
            yield self
        finally:
            self._exit(time.monotonic() - start)

    @contextmanager
    def parked(self):
        """Release the slot while waiting on something external.

        A long-poll spends nearly all of its life here, and none of it is load.
        """
        self._release_to_park()
        try:
            yield
        finally:
            self._resume_from_park()

    # --- internals ---

    def _enter(self) -> None:
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)

    def _exit(self, elapsed: float) -> None:
        with self._lock:
            self._active -= 1
            # Recorded on the way out so a slow request is counted once, with
            # the time it actually took, rather than sampled repeatedly while
            # it is still running.
            self._delays.append(elapsed * 1000.0)

    def _release_to_park(self) -> None:
        with self._lock:
            self._active -= 1
            self._parked += 1

    def _resume_from_park(self) -> None:
        with self._lock:
            self._parked -= 1
            self._active += 1
            self._peak = max(self._peak, self._active)

    def snapshot(self) -> Snapshot:
        with self._lock:
            active, parked, peak = self._active, self._parked, self._peak
            delays = sorted(self._delays)
        return Snapshot(
            active=active,
            parked=parked,
            peak_active=peak,
            delay_p50_ms=round(_quantile(delays, 0.50), 2),
            delay_p95_ms=round(_quantile(delays, 0.95), 2),
            samples=len(delays),
        )

    def reset_peak(self) -> None:
        """Peak is the interesting one between reads, so let a reader clear it."""
        with self._lock:
            self._peak = self._active


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


# --- admission ---------------------------------------------------------------

#: Work classes, coarse on purpose. Too many and each floor is a sliver; too
#: few and the thing you wanted to protect shares a bucket with the flood.
#:
#: The floors are *reservations*, not ceilings. A ceiling only works while
#: there are exactly two classes — with three, two of them can still squeeze
#: the third to nothing. A reservation guarantees a class its share whenever it
#: has work, and unused share is lent to whoever is busy.
CLASS_ADMIT = "admit"   # a room the hub has not seen before
CLASS_WRITE = "write"   # messages, leases, board
CLASS_READ = "read"     # rosters, inboxes, history

RESERVATIONS: dict[str, float] = {
    # Admission is small and must never be starved: a flood of messages
    # blocking new rooms is the cheap attack this exists to prevent.
    CLASS_ADMIT: 0.10,
    CLASS_WRITE: 0.30,
    CLASS_READ: 0.30,
}


class Rejected(Exception):
    """Refused for load. Carries what the caller needs to try again well."""

    def __init__(self, work_class: str, retry_after: float) -> None:
        super().__init__(f"{work_class} is shedding load")
        self.work_class = work_class
        self.retry_after = retry_after


class Admission:
    """Bounds work in flight, and shares it out so no class starves.

    Disabled unless a delay target is set, and it is set by measuring rather
    than guessing — which is why this ships inert. `SWITCHBOARD_LOAD_TARGET_MS`
    turns it on.

    The limit is not configured; it is *discovered*. Concurrency rises while
    queueing delay sits under target and falls when it does not, so capacity
    tracks the machine rather than a constant that goes stale. AIMD rather than
    a threshold switch, because a bare threshold oscillates — admit, overload,
    refuse, idle, admit — and under attack that reads as flapping availability.
    """

    #: Never squeeze below this, or a hub under sustained pressure converges on
    #: serving nobody, which is worse than serving slowly.
    MIN_LIMIT = 4
    MAX_LIMIT = 512

    def __init__(self, meter: LoadMeter, target_ms: float = 0.0) -> None:
        self._meter = meter
        self._target_ms = target_ms
        self._lock = threading.Lock()
        self._limit = float(self.MAX_LIMIT if not target_ms else 32)
        self._in_flight: dict[str, int] = {c: 0 for c in RESERVATIONS}

    @property
    def enabled(self) -> bool:
        return self._target_ms > 0

    def _slots_for(self, work_class: str, total_in_flight: int) -> int:
        """How many slots this class may hold right now.

        Its reservation always, plus a share of whatever nobody else is using —
        so a quiet hub lets one class have everything, and a busy one still
        cannot take another's floor.
        """
        limit = int(self._limit)
        reserved = max(1, int(limit * RESERVATIONS[work_class]))
        others_reserved = sum(
            max(1, int(limit * share))
            for cls, share in RESERVATIONS.items()
            if cls != work_class
        )
        spare = max(0, limit - others_reserved - self._in_flight[work_class])
        return reserved + min(spare, limit - total_in_flight)

    @contextmanager
    def admit(self, work_class: str):
        """Take a slot for this class, or raise `Rejected`."""
        if not self.enabled:
            yield
            return
        with self._lock:
            total = sum(self._in_flight.values())
            if self._in_flight[work_class] >= self._slots_for(work_class, total):
                raise Rejected(work_class, retry_after=self._retry_after())
            self._in_flight[work_class] += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight[work_class] -= 1
            self._adapt()

    def _adapt(self) -> None:
        """Move the limit toward whatever holds delay at target."""
        snap = self._meter.snapshot()
        if not snap.samples:
            # No information is not the same as "under target". Probing upward
            # on an unfed meter would ratchet the limit to its maximum and call
            # that a measurement.
            return
        observed = snap.delay_p95_ms
        with self._lock:
            if observed > self._target_ms:
                # Multiplicative decrease: back off fast, because the cost of
                # staying over target is a queue that keeps growing.
                self._limit = max(self.MIN_LIMIT, self._limit * 0.9)
            else:
                # Additive increase: probe upward slowly, so one quiet moment
                # does not undo a correct backoff.
                self._limit = min(self.MAX_LIMIT, self._limit + 0.5)

    def _retry_after(self) -> float:
        return round(min(5.0, max(0.5, self._target_ms / 1000.0)), 2)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "target_ms": self._target_ms,
                "limit": round(self._limit, 1),
                "in_flight": dict(self._in_flight),
            }
