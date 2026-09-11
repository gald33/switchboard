"""First contact: finding a peer before there is any history with them.

Every other timing signal here is built *from* contact — a forecast comes from
your own history in a workspace and rides on a message — so none of it helps
before the first exchange. These tests cover the three things that do: a note
that outlives presence, a slot both sides derive without talking, and a look
that escalates rather than quitting after two minutes.
"""

from __future__ import annotations

import json

import pytest

from switchboard import rendezvous
from switchboard.cli import main
from switchboard.crypto import generate_key
from switchboard.testing import BASE_URL, hub

WS = "rendezvous-ws"


# --- the shared slot --------------------------------------------------------


def test_two_agents_derive_the_same_slot_without_talking():
    """The whole point: convergence with zero communication.

    Both sides hold the workspace token and nobody else does, so hashing it
    gives them the same phase. Nothing is exchanged, so nothing can be missed.
    """
    now = 1_000_000.0
    a = rendezvous.next_slot("shared-token", "design-review", now)
    b = rendezvous.next_slot("shared-token", "design-review", now)
    assert a == b
    assert a > now


def test_different_topics_do_not_pile_onto_one_minute():
    """Otherwise every rendezvous on a busy hub becomes a thundering herd."""
    now = 1_000_000.0
    slots = {
        rendezvous.next_slot("shared-token", topic, now)
        for topic in ("design-review", "migration", "release", "triage")
    }
    assert len(slots) > 1


def test_different_workspaces_do_not_share_a_slot():
    now = 1_000_000.0
    assert rendezvous.next_slot("token-a", "t", now) != rendezvous.next_slot(
        "token-b", "t", now
    )


def test_the_slot_is_stable_across_a_cadence_and_then_advances():
    token, topic = "shared-token", "design-review"
    now = 1_000_000.0
    first = rendezvous.next_slot(token, topic, now)
    # Anywhere before the slot, both sides still agree on the same one.
    assert rendezvous.next_slot(token, topic, first - 1) == first
    # Past it, the next one is exactly one cadence later.
    assert rendezvous.next_slot(token, topic, first + 1) == first + rendezvous.SLOT_SECONDS


def test_clock_skew_is_what_the_hub_anchor_exists_to_remove():
    """Stated as a test because it is the failure mode being prevented.

    Two machines minutes apart computing against their own clocks land on
    different slots and never overlap — the original miss, rebuilt one layer
    down. Anchoring both on the hub's `now` is what keeps them together.
    """
    token, topic = "shared-token", "design-review"
    hub_now = 1_000_000.0
    skewed = hub_now + 240  # a peer whose clock runs four minutes fast

    assert rendezvous.next_slot(token, topic, hub_now) == rendezvous.next_slot(
        token, topic, hub_now
    ), "same clock, same slot"
    assert rendezvous.next_slot(token, topic, skewed) != rendezvous.next_slot(
        token, topic, hub_now
    ), "different clocks diverge — which is why the hub's is used"


# --- the escalating look ----------------------------------------------------


def test_the_backoff_covers_far_more_ground_than_uniform_polling():
    """Five uniform 25s polls cover two minutes. The same count of these
    covers twenty, which is the difference between catching a peer who is
    arriving and concluding the room is empty."""
    full = rendezvous.schedule(10_000)
    assert sum(full) >= 300, f"only covers {sum(full)}s"
    assert full == sorted(full), "gaps must grow, not jitter"


def test_a_small_budget_keeps_the_early_checks():
    """Truncated, not scaled: catching a peer who is already here is worth
    more than spreading thinly across whatever time was allowed."""
    short = rendezvous.schedule(20)
    assert short[0] == 0.0, "always look immediately"
    assert sum(short) <= 20


def test_a_zero_budget_still_looks_once():
    assert rendezvous.schedule(0) == [0.0]


# --- intent -----------------------------------------------------------------


def test_intent_round_trips():
    note = rendezvous.Intent(
        agent_id="alice", topic="design-review", want="a reviewer",
        since=100.0, looking_until=2000.0, next_slot=300.0,
    )
    again = rendezvous.Intent.from_json(json.loads(json.dumps(note.as_json())))
    assert again == note


def test_a_note_whose_author_gave_up_is_not_worth_answering():
    """Sending a newcomer to wait on somebody who stopped hours ago is the
    same wasted turn this whole command exists to prevent."""
    note = rendezvous.Intent(
        agent_id="alice", topic="t", want="", since=0.0,
        looking_until=1000.0, next_slot=0.0,
    )
    assert note.still_looking(999.0)
    assert not note.still_looking(1001.0)


def test_junk_on_the_board_is_ignored_rather_than_crashing():
    for junk in (None, "a string", {}, {"agent_id": ""}, [1, 2]):
        assert rendezvous.Intent.from_json(junk) is None


# --- end to end through the CLI ---------------------------------------------


@pytest.fixture
def cli_hub(monkeypatch):
    """Encrypted, because that is where this command actually runs.

    The first version of this fixture had no key, and two bugs lived through
    it: prefix listings were dead in an encrypted room, so notes were never
    found at all, and a peer whose name would not open was counted as somebody
    you had met. Both are invisible on a keyless hub — there are no blinded
    identifiers and no unreadable peers — so neither test failed.
    """
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        monkeypatch.setenv("SWITCHBOARD_KEY", key)
        handle.workspace_key = key
        yield handle


def _run(args, capsys):
    code = main(["--url", BASE_URL, "-w", WS, "--json", "rendezvous", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_the_first_arrival_leaves_a_note_and_reports_the_slot(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _run(["design-review", "--want", "a reviewer", "--wait", "0"], capsys)
    assert out["met"] is False
    assert out["next_slot_in"] > 0


def test_a_later_arrival_finds_the_note_when_the_roster_is_empty(
    cli_hub, capsys, monkeypatch
):
    """The case the whole feature is for. Presence lapses in two minutes; the
    note lasts a day, so an agent arriving after the other has gone quiet still
    learns that somebody is looking for it."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "need a reviewer", "--wait", "0"], capsys)

    cli_hub.advance(600)  # alice's presence is long gone

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["met"] is True, "an empty roster must not read as an empty room"
    wants = [n["want"] for n in out["notes"]]
    assert "need a reviewer" in wants


def test_an_expired_note_is_not_presented_as_a_live_peer(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "gone", "--wait", "0", "--until", "60"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["notes"] == [], "alice stopped looking; do not send bob to wait on her"


def test_an_agent_does_not_find_itself(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "hello", "--wait", "0"], capsys)
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["notes"] == []


# --- the peer you have not met ----------------------------------------------


def test_a_peer_on_another_key_is_not_somebody_you_have_met(
    cli_hub, capsys, monkeypatch
):
    """This command's own failure mode, reproduced inside it.

    Same hub, same workspace, different key: you are both in the roster and
    cannot exchange a word. Counting that as a meeting is exactly the forty
    minutes this whole feature exists to prevent — and worse here, because
    `met` tells the agent to stop looking for the peer it could still find.
    """
    cli_hub.client("stranger", key=generate_key()).register(name="on another key")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _run(["design-review", "--wait", "0"], capsys)

    assert out["roster"] == [], "an unreadable peer is not a peer"
    assert out["met"] is False, "do not stop looking because a stranger is here"


def test_the_mismatch_is_reported_rather_than_silently_dropped(
    cli_hub, capsys, monkeypatch
):
    """Excluding them quietly would trade one silent failure for another: the
    key mismatch is the single most likely reason the peer is missing."""
    cli_hub.client("stranger", key=generate_key()).register(name="on another key")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _run(["design-review", "--wait", "0"], capsys)

    assert len(out["key_mismatches"]) == 1


def test_the_human_output_names_the_command_that_settles_it(
    cli_hub, capsys, monkeypatch
):
    """A roster cannot tell you which room you are in; `join` can."""
    cli_hub.client("stranger", key=generate_key()).register(name="on another key")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    assert main(["--url", BASE_URL, "-w", WS, "rendezvous", "t", "--wait", "0"]) == 0

    out = capsys.readouterr().out
    assert "key mismatch" in out
    assert "switchboard join" in out


def test_a_real_peer_is_still_found_alongside_a_stranger(cli_hub, capsys, monkeypatch):
    """The fix must not throw out the meeting with the mismatch."""
    cli_hub.client("stranger", key=generate_key()).register(name="on another key")
    cli_hub.client("bob").register(name="a real peer")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _run(["design-review", "--wait", "0"], capsys)

    assert out["met"] is True
    assert len(out["roster"]) == 1
    assert len(out["key_mismatches"]) == 1


# --- Meeting with no topic agreed -------------------------------------------
#
# The pair that needs a shared topic most is the pair that cannot have one: an
# agent parked with capacity has no task to name, and the agent who arrives
# with a task cannot guess the name the helper would have picked. These cover
# the reserved topic that closes that gap, and the asymmetry that keeps it
# useful once more than two agents hold the key.


def test_a_helper_and_a_requester_meet_without_agreeing_anything(
    cli_hub, capsys, monkeypatch
):
    """The use case in full: she has a task, he has capacity, neither said a
    topic, and they still land on the same board key."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "pypi releases, cloudflare dns", "--wait", "0"], capsys)

    cli_hub.advance(600)  # the helper's presence has long since lapsed

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    out = _run(["--want", "need a package published", "--wait", "0"], capsys)

    assert out["met"] is True
    assert out["topic"] == rendezvous.OPEN_TOPIC
    # Ids are blinded in an encrypted room, so the note's text is what
    # identifies it — which is also all a requester actually needs.
    assert [n["want"] for n in out["notes"]] == ["pypi releases, cloudflare dns"]
    assert [n["role"] for n in out["notes"]] == [rendezvous.OFFERING]


def test_helpers_do_not_find_each_other_and_call_it_a_meeting(
    cli_hub, capsys, monkeypatch
):
    """A room of idle helpers is a crowd, not a peer. Matching your own role
    would report success and stop the search that could still serve somebody."""
    for name in ("helper-a", "helper-b"):
        monkeypatch.setenv("SWITCHBOARD_AGENT_ID", name)
        _run(["--offer", "anything", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-c")
    out = _run(["--offer", "anything", "--wait", "0"], capsys)
    assert out["notes"] == []
    assert out["met"] is False, "capacity meeting capacity is not a meeting"


def test_a_requester_is_not_matched_with_another_requester(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "asker-a")
    _run(["--want", "help with a migration", "--wait", "0"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "asker-b")
    out = _run(["--want", "help with a release", "--wait", "0"], capsys)
    assert out["notes"] == []


# --- what you read, as against who can answer you ----------------------------
#
# The matching rule and the reading rule were one rule, and that is how an
# agent parked with capacity came to be unable to so much as look at the topic
# it was parked on: `notes: []` whether nobody needed anything or the topic was
# full of other offers, which are opposite facts. Splitting them is `--show`.
# Matching does not move — reading more never makes more of it yours to answer.


def test_reading_and_matching_are_different_questions():
    """The rule in isolation, because both surfaces derive from it and a second
    copy of it is a second answer that can drift from the first."""
    seek = {"role": rendezvous.SEEKING, "topic": rendezvous.OPEN_TOPIC}
    assert rendezvous.roles_shown(None, **seek) == {rendezvous.OFFERING}
    assert rendezvous.roles_shown(rendezvous.SHOW_MATCHES, **seek) == {
        rendezvous.OFFERING
    }
    assert rendezvous.roles_shown(rendezvous.SHOW_ALL, **seek) is None
    assert rendezvous.roles_shown(rendezvous.SHOW_WANTS, **seek) == {
        rendezvous.SEEKING
    }
    assert rendezvous.roles_shown(rendezvous.SHOW_OFFERS, **seek) == {
        rendezvous.OFFERING
    }


def test_an_explicit_side_ignores_which_side_you_are_on():
    """`--show offers` means offers. An agent asking to read the capacity in a
    room is not thereby claiming to be looking for it."""
    for role in (rendezvous.SEEKING, rendezvous.OFFERING):
        assert rendezvous.roles_shown(
            rendezvous.SHOW_OFFERS, role=role, topic=rendezvous.OPEN_TOPIC
        ) == {rendezvous.OFFERING}


def test_a_named_topic_filters_nothing_by_default():
    """Roles are a reserved-topic device, and `matches` respects that."""
    assert rendezvous.roles_shown(
        rendezvous.SHOW_MATCHES, role=rendezvous.SEEKING, topic="design-review"
    ) is None


def test_a_helper_can_read_the_topic_it_is_parked_on(cli_hub, capsys, monkeypatch):
    """The complaint this exists for: an agent with capacity could not see the
    room it was sitting in, so it had no way to tell a quiet topic from one
    where the matching rule had hidden everything."""
    for name in ("helper-a", "helper-b"):
        monkeypatch.setenv("SWITCHBOARD_AGENT_ID", name)
        _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-c")
    out = _run(["--offer", "ci debugging", "--wait", "0", "--show", "all"], capsys)
    assert len(out["notes"]) == 2
    assert out["hidden"]["count"] == 0


def test_reading_a_note_is_not_meeting_its_author(cli_hub, capsys, monkeypatch):
    """The false positive arriving by a door the caller opened itself. Asking
    to see every note must not turn a room of helpers into a meeting — that is
    the same error as matching your own role, one layer along."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-a")
    _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-b")
    out = _run(["--offer", "ci debugging", "--wait", "0", "--show", "all"], capsys)
    assert [n["matches"] for n in out["notes"]] == [False]
    assert out["met"] is False, "capacity reading capacity is still not a meeting"


def test_reading_widely_does_not_end_the_search_early(cli_hub, capsys, monkeypatch):
    """`--show all` widens what comes back. Letting it also end the look would
    spend the whole budget on a crowd and call it done — the failure the role
    split exists to prevent, bought back with the flag meant to help."""
    import switchboard.cli as cli_module

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-a")
    _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    naps: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", naps.append)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-b")
    out = _run(["--offer", "x", "--wait", "60", "--show", "all"], capsys)
    assert len(out["notes"]) == 1, "the note it read must still be reported"
    assert naps, "a note that cannot answer you is not a reason to stop looking"


def test_a_filtered_empty_answer_is_not_an_empty_room(cli_hub, capsys, monkeypatch):
    """`notes: []` has two causes that look identical from the caller's side,
    and they point opposite ways: come back later, or look again without the
    filter. The count is what tells them apart."""
    for name in ("helper-a", "helper-b"):
        monkeypatch.setenv("SWITCHBOARD_AGENT_ID", name)
        _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-c")
    out = _run(["--offer", "more ci debugging", "--wait", "0"], capsys)
    assert out["notes"] == []
    assert out["hidden"] == {"count": 2, "offers": 2, "wants": 0}
    assert out["show"] == rendezvous.SHOW_MATCHES


def test_nothing_hidden_is_reported_as_nothing_hidden(cli_hub, capsys, monkeypatch):
    """The other half of the same claim: the count must be a count, not a
    constant that happens to be right in the interesting case."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "solo")
    out = _run(["--want", "a reviewer", "--wait", "0"], capsys)
    assert out["hidden"] == {"count": 0, "offers": 0, "wants": 0}


def test_show_wants_reads_the_requests_and_hides_the_rest(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "releases", "--wait", "0"], capsys)
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "asker")
    _run(["--want", "a release", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "reader")
    out = _run(["--want", "anything", "--wait", "0", "--show", "wants"], capsys)
    assert [n["want"] for n in out["notes"]] == ["a release"]
    assert out["hidden"] == {"count": 1, "offers": 1, "wants": 0}
    # A seeker reading other seekers has read the room, not found a peer.
    assert [n["matches"] for n in out["notes"]] == [False]


def test_the_default_is_unchanged_by_any_of_this(cli_hub, capsys, monkeypatch):
    """The regression guard for the whole change: an agent that passes no
    `--show` sees exactly what it saw before."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "pypi releases", "--wait", "0"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    out = _run(["--want", "need a package published", "--wait", "0"], capsys)
    assert [n["want"] for n in out["notes"]] == ["pypi releases"]
    assert [n["matches"] for n in out["notes"]] == [True]
    assert out["met"] is True


def test_the_human_output_names_the_flag_that_would_show_them(
    cli_hub, capsys, monkeypatch
):
    """A filter nobody is told about is a silent failure wearing the name of a
    feature. Saying the number is half of it; saying the flag is the half that
    lets the agent act."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-a")
    _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-b")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--offer", "x", "--wait", "0"])
    out = capsys.readouterr().out
    assert "1 more live note" in out
    assert "--show all" in out


def test_a_note_you_asked_to_see_is_not_offered_as_a_peer(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-a")
    _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-b")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--offer", "x",
          "--wait", "0", "--show", "all"])
    out = capsys.readouterr().out
    assert "not a match for you" in out
    assert "reading the topic, not meeting on it" in out
    assert "switchboard dm " not in out, "a crowd must not be handed out as a peer"


def test_a_note_is_labelled_by_what_it_is(cli_hub, capsys, monkeypatch):
    """Never by what the reader assumed it would be. Printing another helper's
    offer as `needs` because the reader was offering is how an agent comes to
    address a peer as though it had asked for something."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-a")
    _run(["--offer", "ci debugging", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper-b")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--offer", "x",
          "--wait", "0", "--show", "all"])
    out = capsys.readouterr().out
    assert "offer " in out and "needs " not in out


def test_the_opening_line_is_told_to_quote_the_note(cli_hub, capsys, monkeypatch):
    """The other half of reaching the wrong agent: a peer reached by mistake
    can only recognise the mistake if the message says what it is answering."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "pypi releases", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--want", "x", "--wait", "0"])
    out = capsys.readouterr().out
    assert "quoting the note you are answering" in out
    assert '"pypi releases"' in out


def test_a_note_with_no_description_still_quotes_something_sayable(cli_hub):
    """A blurb that is empty must not produce an opening line that quotes an
    empty string at somebody."""
    from switchboard.cli import _quoted

    assert _quoted("") == "the note you are answering"
    assert _quoted("  ") == "the note you are answering"
    assert _quoted("releases") == '"releases"'


# --- the convention, which is the half no flag can enforce -------------------
#
# Targeting decides which agent is reached. What the reached agent does about
# it is protocol, and the protocol is the packaged SKILL.md — so the rules that
# stop a mistaken contact becoming two confused agents are gated here rather
# than left to whoever edits the prose next.


def test_the_protocol_says_how_to_open_a_first_contact():
    from switchboard.guidance import skill_text

    text = skill_text()
    assert "Open with where you got them" in text
    assert "quoted in the peer's own words" in text


def test_the_protocol_gives_a_mis_reached_agent_something_to_say():
    """The half that is usually skipped. Attempting a request you cannot serve
    confuses two agents; ignoring it leaves the sender waiting out a slot on an
    answer that is never coming. Declining in one line costs neither."""
    from switchboard.guidance import skill_text

    text = skill_text()
    assert "not me" in text
    assert "Do not silently absorb a request you" in text


def test_the_protocol_says_a_filtered_answer_is_not_an_empty_room():
    from switchboard.guidance import skill_text

    text = skill_text()
    assert "--show" in text
    assert "may be the filter rather than the room" in text


def test_a_named_topic_stays_symmetric(cli_hub, capsys, monkeypatch):
    """The regression guard. Roles are a reserved-topic device; on a topic both
    sides agreed, two seekers are the ordinary case and must still meet."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "a reviewer", "--wait", "0"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _run(["design-review", "--want", "also a reviewer", "--wait", "0"], capsys)
    assert [n["want"] for n in out["notes"]] == ["a reviewer"]


def test_the_open_topic_does_not_share_a_slot_with_a_named_one(cli_hub):
    """Same guard as any other pair of topics: the reserved one is not special
    enough to escape the phase derivation, or every idle agent on the hub would
    wake on the same minute."""
    named = rendezvous.slot_phase("w", "design-review")
    assert rendezvous.slot_phase("w", rendezvous.OPEN_TOPIC) != named


def test_a_note_written_before_roles_existed_reads_as_seeking(cli_hub):
    """Old notes on the board have no role. Reading them as anything but
    seeking would hide them from the helpers who can answer them."""
    note = rendezvous.Intent.from_json({
        "agent_id": "old", "topic": "open", "want": "help",
        "since": 0.0, "looking_until": 9e9, "next_slot": 0.0,
    })
    assert note is not None
    assert note.role == rendezvous.SEEKING


def test_the_human_output_says_the_next_move_is_a_dm(cli_hub, capsys, monkeypatch):
    """Finding the note is an introduction, not a conversation. An agent that
    does not know it may now simply address the peer goes back to waiting."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "releases", "--wait", "0"], capsys)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--want", "x", "--wait", "0"])
    out = capsys.readouterr().out
    assert "switchboard dm " in out
    assert "releases" in out


# --- can this peer actually be woken? ---------------------------------------
#
# `looking_until` is a plan, and a plan written by a turn-based session that
# has since ended looks exactly like one still being kept. A live
# `listener/<id>` is a different kind of claim: a process saying so now, which
# expires on its own the moment that process stops.


def test_a_peer_with_a_listener_parked_is_reported_reachable(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    out = _run(["--offer", "releases", "--wait", "0"], capsys)
    helper_id = out["agent_id"]
    # What `listen` writes while parked, with the TTL that makes it a claim
    # about now rather than about intent.
    main(["--url", BASE_URL, "-w", WS, "board", "set",
          rendezvous.listener_key(helper_id), '{"waiting_on": "inbox"}',
          "--json-body", "--ttl", "90"])
    capsys.readouterr()

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    out = _run(["--want", "a release", "--wait", "0"], capsys)
    assert [n["reachable"] for n in out["notes"]] == [True]


def test_a_peer_with_no_listener_is_not_claimed_to_be_reachable(
    cli_hub, capsys, monkeypatch
):
    """The failure that matters: telling a requester to expect a reply from an
    agent that ended its turn hours ago."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "releases", "--wait", "0"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    out = _run(["--want", "a release", "--wait", "0"], capsys)
    assert [n["reachable"] for n in out["notes"]] == [False]


def test_the_advice_says_whether_to_expect_a_reply_this_turn(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "helper")
    _run(["--offer", "releases", "--wait", "0"], capsys)
    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "requester")
    main(["--url", BASE_URL, "-w", WS, "rendezvous", "--want", "x", "--wait", "0"])
    text = capsys.readouterr().out
    assert "No listener is parked" in text
    assert "do not wait on a reply this turn" in text


def test_the_listener_key_has_one_spelling(cli_hub):
    """`listen` writes it and `rendezvous` reads it. Two spellings would make
    every peer look unreachable, silently and forever."""
    import inspect

    from switchboard import cli as cli_module

    # The key is spelled where a parked room is built, and nowhere else in
    # the listener: not in the per-room loop, not in the process that runs it.
    assert "rendezvous.listener_key(" in inspect.getsource(cli_module._Post)
    for piece in (cli_module.cmd_listen, cli_module._park, cli_module._listen_posts):
        assert 'f"listener/{' not in inspect.getsource(piece)
        assert "listener/" not in inspect.getsource(piece)
