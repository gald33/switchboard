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
