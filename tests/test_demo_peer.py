"""The demo room's resident agent (`extras/chatgpt-plugin/demo_peer.py`).

Reviewers run the plugin's test cases against it, so what it promises is what
the test cases expect: it is on the roster, holds the README, keeps a plan on
the board, posts on general, and answers every direct message.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from switchboard.client import LeaseHeld
from switchboard.crypto import generate_key
from switchboard.testing import hub as make_hub

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "demo_peer", ROOT / "extras" / "chatgpt-plugin" / "demo_peer.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


@pytest.fixture
def room():
    with make_hub(workspace="demo-room", key=generate_key()) as handle:
        yield handle


def test_one_pass_sets_up_everything_a_reviewer_will_look_for(room):
    peer = demo.DemoPeer(room.client("demo-teammate"))
    peer.tick(wait=0)
    reviewer = room.client("reviewer", register=True, channels=["general"])
    (agent,) = [a for a in reviewer.agents() if a["name"] == demo.NAME]
    assert agent["task"] == f"editing {demo.RESOURCE}"
    # Resource names reach the hub blinded, so what a reviewer meets is the
    # conflict: claiming the README says the demo teammate holds it.
    with pytest.raises(LeaseHeld) as held:
        reviewer.acquire(demo.RESOURCE)
    assert held.value.payload["holder"] == peer.client.agent_id
    assert reviewer.board_get(demo.PLAN_KEY) == demo.PLAN
    posts = reviewer.inbox()
    assert any(demo.NAME in str(m["body"]) for m in posts)


def test_every_direct_message_gets_an_answer(room):
    peer = demo.DemoPeer(room.client("demo-teammate"))
    peer.tick(wait=0)
    reviewer = room.client("reviewer", register=True)
    reviewer.send(peer.client.agent_id, "are you working on the README?")
    assert peer.tick(wait=0) == 1
    (answer,) = [m for m in reviewer.inbox() if str(m["channel"]).startswith("@")]
    assert "are you working on the README?" in answer["body"]
    assert demo.RESOURCE in answer["body"]


def test_it_refreshes_only_when_due():
    now = {"t": 0.0}

    class Fake:
        agent_id = "me"

        def __init__(self):
            self.posts = 0

        def heartbeat(self, **_):
            pass

        def acquire(self, *a, **k):
            pass

        def board_set(self, *a, **k):
            pass

        def post(self, *a, **k):
            self.posts += 1

        def inbox(self, **_):
            return []

    fake = Fake()
    peer = demo.DemoPeer(fake, clock=lambda: now["t"])
    peer.tick(wait=0)
    peer.tick(wait=0)
    assert fake.posts == 1
    now["t"] = demo.REFRESH_SECONDS + 1
    peer.tick(wait=0)
    assert fake.posts == 2
