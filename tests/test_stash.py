"""A message that could not be opened is not a message that is gone.

The cursor advances whether or not a body opened, and the one moment a caller
is guaranteed to lack a peer's key is that peer's first message. So the default
read destroyed, specifically and reliably, first contact from anyone not
already known — and reading the roster afterwards did not bring it back.
"""

from __future__ import annotations

import sqlite3
import time

from switchboard.stash import RETENTION_SECONDS, UnopenedStash
from switchboard.testing import hub as make_hub

WS = "stash-tests"


def _stashed(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM unopened").fetchone()[0]
    finally:
        conn.close()


def test_a_whisper_that_could_not_be_opened_survives_the_cursor(tmp_path):
    """The 2026-09-07 loss, replayed — and recovered this time."""
    with make_hub(workspace=WS, stash_db=str(tmp_path / "stash.db")) as h:
        mgr, trader = h.client("manager"), h.client("trader")
        mgr.register(name="manager")
        trader.register(name="trader")
        mgr.agents()
        mgr.whisper(trader.agent_id, "your capacities: salt 1.5894 per labour")

        # A reader with no key for the sender, and no way to learn one: the
        # sender has left the roster, which is why a resend was never coming.
        trader._peer_exchange_keys.clear()
        trader._refreshed_for_read = True
        [blind] = trader.inbox()
        assert blind["unreadable"] is True
        assert trader.inbox() == [], "the hub has moved the cursor past it"

        # The key arrives later. One minute or one day: the stash does not care.
        trader._peer_exchange_keys[mgr.agent_id] = mgr.exchange_key
        [recovered] = trader.inbox()
        assert recovered["recovered"] is True
        assert recovered["body"] == "your capacities: salt 1.5894 per labour"
        assert "unreadable" not in recovered


def test_recovering_it_once_is_enough(tmp_path):
    """A recovered message is delivered, not a permanent fixture of every read."""
    with make_hub(workspace=WS, stash_db=str(tmp_path / "stash.db")) as h:
        mgr, trader = h.client("manager"), h.client("trader")
        mgr.register(name="manager")
        trader.register(name="trader")
        mgr.agents()
        mgr.whisper(trader.agent_id, "once")

        trader._peer_exchange_keys.clear()
        trader._refreshed_for_read = True
        trader.inbox()
        trader._peer_exchange_keys[mgr.agent_id] = mgr.exchange_key
        assert len(trader.inbox()) == 1
        assert trader.inbox() == [], "it must not come back on every subsequent read"


def test_what_still_cannot_be_opened_is_kept_for_the_next_try(tmp_path):
    path = str(tmp_path / "stash.db")
    with make_hub(workspace=WS, stash_db=path) as h:
        mgr, trader = h.client("manager"), h.client("trader")
        mgr.register(name="manager")
        trader.register(name="trader")
        mgr.agents()
        mgr.whisper(trader.agent_id, "still sealed")

        trader._peer_exchange_keys.clear()
        trader._refreshed_for_read = True
        trader.inbox()
        assert _stashed(path) == 1
        trader.inbox()                      # a read with the key still unknown
        assert _stashed(path) == 1, "a failed attempt must not discard it"


def test_the_stash_is_off_unless_asked_for(tmp_path):
    """Off unless asked for — and never in the home directory during a test.

    Writing to `~/.switchboard/stash.db` from a test suite is how a case ends
    up reading somebody else's sealed message, and how a developer's own stash
    acquires rows from a test run. It happened once, while writing these.
    """
    with make_hub(workspace=WS) as h:
        mgr, trader = h.client("manager"), h.client("trader")
        mgr.register(name="manager")
        trader.register(name="trader")
        mgr.agents()
        mgr.whisper(trader.agent_id, "not kept")
        trader._peer_exchange_keys.clear()
        trader._refreshed_for_read = True
        assert trader._stash is None
        [blind] = trader.inbox()
        assert blind["unreadable"] is True   # still safe, just not recoverable


def test_it_forgets_what_nobody_came_back_for(tmp_path):
    """A stash that grew forever would contradict the one idea here."""
    path = str(tmp_path / "stash.db")
    s = UnopenedStash(path)
    s.put(WS, "agent", {"seq": 1, "from": "x", "body": "sealed"})
    assert _stashed(path) == 1
    s.prune(now=time.time() + RETENTION_SECONDS + 1)
    assert _stashed(path) == 0
    s.close()


def test_an_unwritable_home_costs_the_recovery_not_the_command(tmp_path):
    """Every method fails soft — see the module docstring."""
    s = UnopenedStash(str(tmp_path / "no" / "such" / "dir" / "x.db"))
    (tmp_path / "no").write_text("a file where a directory would go")
    s.put(WS, "agent", {"seq": 1, "from": "x", "body": "sealed"})   # must not raise
    assert s.recover(WS, "agent", lambda m: True) == []
    s.prune()
