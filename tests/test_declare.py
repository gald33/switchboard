"""The lease and the board did not reinforce each other.

Found by a subagent given a deliberately thin brief and asked to coordinate the
way the skill says to. It read the board, found `rewriting the lexer in
src/parser.py — do not touch it, I am mid-rewrite`, and then took the lease on
that exact file anyway. Nothing it did was wrong: the lease is the enforced
surface, the board is the durable one, and the enforced one had never heard of
the durable one. An agent that checks leases instead of the board — which is the
cheaper, more obvious check — gets a green light past a standing "mine".

The fix is not to merge them. They answer different questions on different
clocks, and a lease long enough to carry durable intent is the leaked hold the
short TTL exists to prevent. The fix is that `claim` now *reads* the durable
surface and says what it found, and `release` clears its own.

Advisory throughout. A declaration left behind by a session that died must not
be able to make a file permanently unclaimable.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import main
from switchboard.client import Identity
from switchboard.crypto import generate_key
from switchboard.holds import HOLDS_PREFIX
from switchboard.mcp_server import Bridge, handle_request
from switchboard.testing import BASE_URL, hub
from switchboard.timing import TimingModel

WS = "declare-ws"
RESOURCE = "src/parser.py"


@pytest.fixture
def cli_hub(monkeypatch):
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        monkeypatch.setenv("SWITCHBOARD_KEY", key)
        yield handle


def _run(*args, code=0):
    assert main(["--url", BASE_URL, "-w", WS, *args]) == code


def _run_json(capsys, *args, code=0):
    capsys.readouterr()  # whatever an earlier `_run` printed is not our JSON
    assert main(["--url", BASE_URL, "-w", WS, "--json", *args]) == code
    return json.loads(capsys.readouterr().out)


def _as(monkeypatch, who):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", who)


# --- the regression itself ---------------------------------------------------


def test_a_standing_declaration_is_reported_when_somebody_else_claims(
    cli_hub, capsys, monkeypatch
):
    """The measured miss. Alice declares on the board; Bob claims the lease.

    Bob still gets the lease — that part was never the bug — but he can no
    longer do it without being told.
    """
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "mid-rewrite of the lexer", "--declare")
    cli_hub.advance(3600)  # alice's turn ended; her lease lapsed, her intent did not
    capsys.readouterr()

    _as(monkeypatch, "bob")
    _run("claim", RESOURCE, "-m", "quick fix")

    err = capsys.readouterr().err
    assert "declared" in err
    assert "alice" in err, "an opaque blinded id is a warning nobody acts on"
    assert "mid-rewrite of the lexer" in err, "the reason is the actionable part"


def test_the_declaration_warns_and_does_not_block(cli_hub, capsys, monkeypatch):
    """Warn-never-block, asserted rather than assumed.

    If this ever exits non-zero, a dead session's leftover note has become a
    permanent lock on a file, which is strictly worse than the bug being fixed.
    """
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "mine for the week", "--declare")
    cli_hub.advance(3600)
    capsys.readouterr()

    _as(monkeypatch, "bob")
    out = _run_json(capsys, "claim", RESOURCE)

    assert out["acquired"] is True
    assert out["standing_hold"]["who"] == "alice"
    assert out["standing_hold"]["intent"] == "mine for the week"


def test_a_json_caller_is_told_too(cli_hub, capsys, monkeypatch):
    """The warning goes to stderr, which a `--json` caller is entitled to
    ignore. Putting it only there would fix the CLI and leave the MCP bridge
    and every scripted agent exactly as blind as before."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "hands off", "--declare")
    cli_hub.advance(3600)
    capsys.readouterr()

    _as(monkeypatch, "bob")
    out = _run_json(capsys, "claim", RESOURCE)
    assert out["standing_hold"] is not None
    assert capsys.readouterr().err == "", "--json speaks on stdout only"


def test_your_own_declaration_does_not_warn_you(cli_hub, capsys, monkeypatch):
    """A warning you always get is a warning you stop reading. Reclaiming
    across turns is the normal path for the agent that declared."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "long job", "--declare")
    cli_hub.advance(3600)
    capsys.readouterr()

    out = _run_json(capsys, "claim", RESOURCE)
    assert out["standing_hold"] is None
    assert "declared" not in capsys.readouterr().err


def test_no_declaration_means_no_noise(cli_hub, capsys, monkeypatch):
    _as(monkeypatch, "bob")
    _run("claim", RESOURCE, "-m", "ordinary work")
    assert "declared" not in capsys.readouterr().err


# --- what `--declare` writes, and how long it lasts --------------------------


def test_declare_writes_a_hold_at_a_predictable_key(cli_hub, capsys, monkeypatch):
    """The key shape is the contract: an agent that wants to read declarations
    without running `claim` — a reaper, the viewer, a human — needs to know
    where they are without guessing."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "rewriting", "--declare")
    capsys.readouterr()

    held = cli_hub.client("alice").board_get(HOLDS_PREFIX + RESOURCE)
    assert held["resource"] == RESOURCE
    assert held["who"] == "alice"
    assert held["intent"] == "rewriting"


def test_a_declaration_outlives_the_lease_that_created_it(cli_hub, capsys, monkeypatch):
    """The entire reason it is a second surface. The lease lapses in minutes
    because renewal is a heartbeat side effect; the intent does not."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "multi-day port", "--declare")
    capsys.readouterr()

    cli_hub.advance(4 * 3600)  # every plausible lease TTL has expired
    assert cli_hub.client("alice").leases() == [], "the lease is genuinely gone"

    _as(monkeypatch, "bob")
    out = _run_json(capsys, "claim", RESOURCE)
    assert out["acquired"] is True, "an expired lease is free to take"
    assert out["standing_hold"]["intent"] == "multi-day port", "the intent is not"


def test_declaring_is_opt_in(cli_hub, capsys, monkeypatch):
    """Most claims are the seconds-long read-modify-write the lease was built
    for. Declaring those would fill the board with holds nobody meant."""
    _as(monkeypatch, "alice")
    out = _run_json(capsys, "claim", RESOURCE, "-m", "a quick edit")

    assert out["declared"] is False
    assert cli_hub.client("alice").board_get(HOLDS_PREFIX + RESOURCE) is None


def test_the_declaration_is_stamped_on_the_hubs_clock(cli_hub, capsys, monkeypatch):
    """Two agents comparing notes are exactly the two whose local clocks
    disagree, so `since` comes off the lease the hub just issued."""
    _as(monkeypatch, "alice")
    out = _run_json(capsys, "claim", RESOURCE, "-m", "x", "--declare")
    held = cli_hub.client("alice").board_get(HOLDS_PREFIX + RESOURCE)
    assert held["since"] == out["acquired_at"]


# --- clearing up -------------------------------------------------------------


def test_release_clears_your_own_declaration(cli_hub, capsys, monkeypatch):
    """It lasts a day. The work usually does not, and a hold with nobody behind
    it is the litter this is meant to prevent rather than create."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "done shortly", "--declare")
    out = _run_json(capsys, "release", RESOURCE)

    assert out["declaration_cleared"] is True
    assert cli_hub.client("alice").board_get(HOLDS_PREFIX + RESOURCE) is None


def test_releasing_without_a_declaration_says_so_rather_than_failing(
    cli_hub, capsys, monkeypatch
):
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE)
    out = _run_json(capsys, "release", RESOURCE)

    assert out["released"] is True
    assert out["declaration_cleared"] is False


def test_release_does_not_clear_somebody_elses_declaration(
    cli_hub, capsys, monkeypatch
):
    """`--force` breaks somebody else's *lease*, because a lease is a claim
    about a live process and that claim can be wrong. A declaration is a claim
    about intent, which force knows nothing about."""
    _as(monkeypatch, "alice")
    _run("claim", RESOURCE, "-m", "still mine", "--declare")
    capsys.readouterr()

    _as(monkeypatch, "bob")
    out = _run_json(capsys, "release", RESOURCE, "--force")

    assert out["released"] is True, "bob did break alice's lease"
    assert out["declaration_cleared"] is False
    held = cli_hub.client("alice").board_get(HOLDS_PREFIX + RESOURCE)
    assert held["intent"] == "still mine", "and alice's intent survived it"


# --- the board must never be able to fail a claim ----------------------------


def test_a_board_failure_does_not_fail_the_claim(cli_hub, capsys, monkeypatch):
    """The lease is the enforced surface. Making it depend on a second,
    advisory one would trade a missed warning for an outage."""
    import switchboard.cli as cli_module
    from switchboard.client import SwitchboardError

    broken = cli_module.Client

    class BoardDown(broken):  # type: ignore[misc, valid-type]
        def board_get(self, *a, **kw):
            raise SwitchboardError("board is down")

    monkeypatch.setattr(cli_module, "Client", BoardDown)
    _as(monkeypatch, "bob")
    out = _run_json(capsys, "claim", RESOURCE)

    assert out["acquired"] is True
    assert out["standing_hold"] is None


def test_a_declaration_that_will_not_write_is_not_a_silent_no_op(
    cli_hub, capsys, monkeypatch
):
    """You asked for two things and got one. The claim is real and must not be
    reported as failed — but a declaration you believe exists and does not is
    worse than never having asked."""
    import switchboard.cli as cli_module
    from switchboard.client import SwitchboardError

    class WriteDown(cli_module.Client):  # type: ignore[misc, valid-type]
        def board_set(self, *a, **kw):
            raise SwitchboardError("read-only board")

    monkeypatch.setattr(cli_module, "Client", WriteDown)
    _as(monkeypatch, "alice")
    out = _run_json(capsys, "claim", RESOURCE, "-m", "x", "--declare")

    assert out["acquired"] is True
    assert out["declared"] is False


# --- the MCP surface ---------------------------------------------------------
#
# Not a copy of the CLI tests for symmetry's sake. The bug being fixed is that
# the enforced surface never mentioned the advisory one, and an MCP agent is
# exactly as able to make that mistake as a shell one — a fix that lands only in
# `cli.py` leaves half the audience where it started.


def _bridge(handle, who):
    """One bridge, built by hand.

    Deliberately not imported from `test_mcp.py`. Cross-importing test modules
    works under `python -m pytest`, which puts the working directory on
    `sys.path`, and fails under bare `pytest`, which does not — so it passes
    locally and 404s in CI, which is how this arrived. Twelve duplicated lines
    cost less than a test that only runs one of the two ways it is invoked.
    """
    bridge = Bridge.__new__(Bridge)
    bridge.config = handle.client_config(agent_id=who)
    bridge.identity = Identity(agent_id=who, name=who, kind="local",
                               branch=f"feat/{who}", meta={})
    bridge.client = handle.client(who, agent_id=who)
    bridge.timing = TimingModel(":memory:")
    bridge._registered = False
    return bridge


@pytest.fixture
def bridge_pair():
    """Two bridges on one workspace, which is the situation the bug needs."""
    key = generate_key()
    with hub(workspace="declare-mcp", key=key) as handle:
        yield handle, (lambda who: _bridge(handle, who))


def _mcp(bridge, name, **kw):
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": kw},
    })
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    assert not result.get("isError", False), payload
    return payload


def test_mcp_claim_reports_a_standing_declaration(bridge_pair):
    handle, make = bridge_pair
    alice, bob = make("alice"), make("bob")

    assert _mcp(alice, "claim", resource=RESOURCE,
                note="mid-rewrite", declare=True)["declared"] is True
    handle.advance(3600)  # alice's lease lapsed; her intent did not

    out = _mcp(bob, "claim", resource=RESOURCE)
    assert out["acquired"] is True, "advisory, never blocking — here too"
    assert out["standing_hold"]["intent"] == "mid-rewrite"
    assert "alice" in out["advice"]


def test_mcp_claim_is_quiet_when_there_is_nothing_to_say(bridge_pair):
    _, make = bridge_pair
    out = _mcp(make("bob"), "claim", resource=RESOURCE)
    assert "standing_hold" not in out
    assert out["declared"] is False


def test_mcp_release_clears_your_own_declaration(bridge_pair):
    _, make = bridge_pair
    alice = make("alice")
    _mcp(alice, "claim", resource=RESOURCE, note="a while", declare=True)

    assert _mcp(alice, "release", resource=RESOURCE)["declaration_cleared"] is True
    assert alice.client.board_get(HOLDS_PREFIX + RESOURCE) is None


def test_declaring_into_a_custom_scope_is_refused_rather_than_misfiled(bridge_pair):
    """A custom scope is a different workspace and key; the blackboard has no
    custom-scope form. Writing the note anyway would put it in the default
    workspace, where nobody in the private conversation can see it — a
    declaration you believe exists and does not is worse than none."""
    _, make = bridge_pair
    alice = make("alice")

    out = _mcp(alice, "claim", resource=RESOURCE, note="private work", declare=True,
               custom_scope={"workspace": "w_elsewhere", "key": generate_key()})

    assert out["acquired"] is True
    assert out["declared"] is False
    assert "custom-scope" in out["declare_note"]
    assert alice.client.board_get(HOLDS_PREFIX + RESOURCE) is None
