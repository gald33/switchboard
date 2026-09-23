"""`POST /messages/hold`, over HTTP: deferring a message is not losing it.

A listener on do-not-disturb leaves ordinary direct messages unread, and the
hub expires unread messages on the same one-hour clock as read ones. The store
tests pin the arithmetic; these pin that the surface an agent calls reaches it,
in both kinds of room, and reaches only the caller's own mail.
"""

from __future__ import annotations

import pytest

from switchboard.crypto import generate_key
from switchboard.testing import hub as make_hub


@pytest.fixture(params=["plaintext", "encrypted"])
def room(request):
    key = generate_key() if request.param == "encrypted" else None
    with make_hub(workspace="hold-ws", key=key) as handle:
        yield handle


def test_held_mail_outlives_its_own_ttl(room):
    busy, peer = room.client("busy"), room.client("peer")
    busy.register(name="busy")
    peer.send(busy.agent_id, "read me when you are free", ttl=60)

    result = busy.hold_dms(7200)
    assert result["held"] == 1
    assert result["until"]

    room.advance(3600)
    assert [m["body"] for m in busy.inbox()] == ["read me when you are free"]


def test_without_a_hold_the_same_mail_is_gone(room):
    """The failure the endpoint exists for, so the test above is not passing
    against a hub that never expired anything."""
    busy, peer = room.client("busy"), room.client("peer")
    busy.register(name="busy")
    peer.send(busy.agent_id, "lost", ttl=60)
    room.advance(3600)
    assert busy.inbox() == []


def test_a_hold_reaches_only_the_callers_own_mail(room):
    busy, other, peer = room.client("busy"), room.client("other"), room.client("peer")
    peer.send(other.agent_id, "not yours to keep", ttl=60)
    assert busy.hold_dms(7200)["held"] == 0
    room.advance(120)
    assert other.inbox() == []
