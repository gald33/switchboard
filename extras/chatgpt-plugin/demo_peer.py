#!/usr/bin/env python3
"""A resident agent for the plugin's demo room, so reviewers meet somebody.

OpenAI reviews a plugin by running its test cases against demo credentials,
possibly days apart, on web and mobile. A Switchboard room is empty unless an
agent is in it — presence lasts two minutes, messages an hour — so a test
like "who else is working here?" needs someone who stays. This is that
someone, and nothing more:

- it stays on the roster as "Demo teammate", working on `docs/README.md`;
- it holds a claim on `docs/README.md`, so "claim the README" meets a holder;
- it keeps a plan on the board at `demo/plan`;
- it posts on `general` every so often, so the channel is never empty;
- it answers every direct message with a short reply naming what it got.

Run it next to the bridge, with the demo room's invite:

    SWITCHBOARD_INVITE=swb1_... python3 extras/chatgpt-plugin/demo_peer.py

It keeps nothing on disk. Stop it when the review is over; the room forgets it
on its own within minutes.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

from switchboard.client import Client, SwitchboardError

NAME = "Demo teammate"
RESOURCE = "docs/README.md"
PLAN_KEY = "demo/plan"
PLAN = {
    "goal": "Tidy the project README",
    "done": ["intro rewritten", "install section checked"],
    "next": ["examples section", "link check"],
    "owner": NAME,
}
#: How often to repost on `general` and rewrite the board. Well inside the
#: hour a message lives and the day a board entry lives.
REFRESH_SECONDS = 45 * 60


def _log(message: str) -> None:
    print(f"[demo-peer] {message}", file=sys.stderr, flush=True)


def reply_to(message: dict[str, Any]) -> str:
    body = message.get("body")
    text = body if isinstance(body, str) else "your message"
    if len(text) > 120:
        text = text[:117] + "..."
    return (f"Hi, I'm the demo teammate in this room. I got your message: \"{text}\". "
            f"I'm working on {RESOURCE}; the plan is on the board at {PLAN_KEY}.")


class DemoPeer:
    def __init__(self, client: Client, clock=time.monotonic) -> None:
        self.client = client
        self._clock = clock
        self._last_refresh: float | None = None

    def announce(self) -> None:
        self.client.register(name=NAME, kind="cloud", task=f"editing {RESOURCE}",
                             channels=["general"], ttl=300)

    def refresh(self) -> None:
        """The durable parts: the claim, the board entry, a line on general."""
        try:
            self.client.acquire(RESOURCE, note="demo: tidying the README", ttl=3600)
        except SwitchboardError as exc:
            _log(f"claim: {exc}")          # someone else holds it; that's fine too
        self.client.board_set(PLAN_KEY, PLAN, ttl=24 * 3600)
        self.client.post("general", f"{NAME}: still here, working on {RESOURCE}. "
                                    "DM me and I'll answer.")
        self._last_refresh = self._clock()

    def tick(self, wait: float = 25.0) -> int:
        """One pass: stay present, refresh when due, answer every DM. Returns
        how many messages were answered."""
        try:
            self.client.heartbeat(task=f"editing {RESOURCE}", ttl=300)
        except SwitchboardError:
            self.announce()                # presence lapsed, or first run
        if self._last_refresh is None or self._clock() - self._last_refresh > REFRESH_SECONDS:
            self.refresh()
        answered = 0
        for message in self.client.inbox(wait=wait):
            sender = message.get("from")
            if not sender or sender == self.client.agent_id:
                continue
            if str(message.get("channel", "")).startswith("@"):
                self.client.send(sender, reply_to(message))
                answered += 1
        return answered


def main() -> int:
    blob = os.environ.get("SWITCHBOARD_INVITE")
    if not blob:
        _log("set SWITCHBOARD_INVITE to the demo room's invite")
        return 2
    # Nothing on disk: the peer-key log and the unopened-message stash are off.
    os.environ.setdefault("SWITCHBOARD_PEER_DB", "")
    os.environ.setdefault("SWITCHBOARD_STASH_DB", "")
    client = Client.from_invite(blob, agent_id="demo-teammate")
    peer = DemoPeer(client)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    peer.announce()
    _log(f"in {client.workspace} as {client.agent_id}")
    while not stop["now"]:
        try:
            answered = peer.tick()
            if answered:
                _log(f"answered {answered} message(s)")
        except (SwitchboardError, OSError) as exc:
            _log(f"hub error, retrying: {exc}")
            time.sleep(10)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
