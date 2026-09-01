"""Retiring an identity that is no longer yours.

Reported from a live room, three times in one day: an agent read its own id
off the roster, passed it to `leave`, and was told "was not on the roster"
while the roster went on printing it. The cause is that a roster id has
already been blinded, and passing it back blinded it a second time.

It matters because identities drift. A `git checkout` re-derives an agent id
(see `identity-rebinds-on-branch-change`), stranding the old one — and with
`--back-in` set, the ghost keeps advertising a return it will never make.
"""

from __future__ import annotations

import pytest

from switchboard.testing import hub


@pytest.fixture
def h():
    with hub(workspace="ghosts") as handle:
        yield handle


def test_an_agent_can_retire_an_id_it_no_longer_holds(h):
    ghost = h.client("on-branch-one")
    ghost.register(name="worker", task="before the checkout", ttl=120)
    drifted = h.client("on-branch-two")

    listed = next(a["agent_id"] for a in drifted.agents())
    assert drifted.deregister(agent_id=listed) is True
    assert drifted.agents() == []


def test_retiring_yourself_still_needs_no_argument(h):
    agent = h.client("just-me")
    agent.register(name="worker", ttl=120)

    assert agent.deregister() is True
    assert agent.agents() == []


def test_the_id_from_the_roster_is_used_verbatim(h):
    """The whole bug in one assertion: what the roster prints is a wire id, and
    a wire id must not be transformed again on its way back."""
    ghost = h.client("on-branch-one")
    ghost.register(name="worker", ttl=120)
    listed = next(a["agent_id"] for a in h.client("other").agents())

    # Its own id, blinded once. Passing it through the client a second time is
    # what produced "was not on the roster" against a roster showing it.
    assert h.client("other").deregister(agent_id=listed) is True
