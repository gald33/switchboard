#!/usr/bin/env python3
"""Write `chatgpt-app-submission.json`, the file OpenAI's portal imports.

The portal's import format
(https://developers.openai.com/plugins/schemas/chatgpt-app-submission.v1.json)
fills the listing, every tool's annotations with a justification for each,
and the test cases. The annotations are read from the hosted bridge's own
tool list, the one `Scan Tools` will find, so the file cannot claim a hint
the server does not declare. What is written here by hand is the reasoning
behind each hint, and the test cases.

    python3 extras/chatgpt-plugin/build_submission.py       # rewrites the file

`tests/test_chatgpt_plugin_package.py` fails when the committed file is not
what this script writes, or does not validate against the schema.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "chatgpt-app-submission.json"
SCHEMA_URL = "https://developers.openai.com/plugins/schemas/chatgpt-app-submission.v1.json"
CATEGORIES = {"Developer Tools": "DEVELOPER_TOOLS", "Productivity": "PRODUCTIVITY"}

#: Every tool acts in one Switchboard room: a bounded, private workspace
#: reached only through the hub the bridge allows. None reaches the open web.
_BOUNDED = ("Acts only inside the Switchboard room the user joined, a private workspace "
            "on the one hub this bridge serves. It does not reach the public internet or "
            "any other service.")

#: tool -> (what it does to state, why that is or is not irreversible).
#: Read-only tools say what they read; the rest say what they change.
REASONS: dict[str, tuple[str, str]] = {
    "help": ("Returns the coordination protocol text bundled with the server. Reads nothing "
             "from the room and changes nothing.",
             "Changes nothing."),
    "whoami": ("Reports this agent's identity and connection: agent id, room, hub. Reads "
               "only; changes nothing.",
               "Changes nothing."),
    "roster": ("Lists the agents currently present in the room and what they say they are "
               "doing. Reads only; changes nothing.",
               "Changes nothing."),
    "claims": ("Lists the live leases in the room: what is taken, by whom, until when. "
               "Reads only; changes nothing.",
               "Changes nothing."),
    "history": ("Reads recent messages on a channel without moving the read cursor. Reads "
                "only; changes nothing.",
                "Changes nothing."),
    "board_get": ("Reads one entry from the room's shared board. Reads only; changes "
                  "nothing.",
                  "Changes nothing."),
    "board_list": ("Lists the keys on the room's shared board. Reads only; changes nothing.",
                   "Changes nothing."),
    "keygen": ("Generates a fresh random key locally and returns it. Nothing is sent to the "
               "hub or stored; no room or state changes.",
               "Changes nothing."),
    "linked_keys": ("Lists the ids of the keys the user linked by signing in, never the keys "
                    "themselves. Reads only; changes nothing.",
                    "Changes nothing."),
    "checkin": ("Writes a heartbeat so the agent stays on the roster, renews the agent's own "
                "leases, and moves its read cursor past the messages it returns.",
                "Everything it writes expires on its own within minutes, and it only extends "
                "the agent's own presence and leases. Nothing is deleted or sent to others."),
    "claim": ("Takes a lease on a named resource, which other agents see on the roster.",
              "The lease is the agent's own and expires on its own (15 minutes by default) "
              "or when released. It never takes a lease another agent holds."),
    "release": ("Gives up a lease the agent holds.",
                "Only ever releases the agent's own lease, and the resource can be claimed "
                "again at once."),
    "renew": ("Extends a lease the agent already holds.",
              "Only lengthens the agent's own lease, which still expires on its own."),
    "inbox": ("Returns waiting messages and moves the agent's own read cursor past them.",
              "Moves only this agent's read position; the messages stay in the room for "
              "everyone else, and `history` can still read them."),
    "subscribe": ("Adds channels to what this agent's inbox reads.",
                  "Changes only this agent's own reading list, and `unsubscribe` undoes it."),
    "unsubscribe": ("Removes channels from what this agent's inbox reads.",
                    "Changes only this agent's own reading list, and `subscribe` undoes it."),
    "join_room": ("Connects this conversation to a Switchboard room, from an invite or a "
                  "linked room's name, and registers the agent there.",
                  "Joining writes only the agent's own presence, which lapses by itself in "
                  "minutes. Calling it again with another room moves the conversation."),
    "say": ("Posts a message to a channel that every agent subscribed to it can read.",
            "A sent message cannot be recalled, and other agents may act on it."),
    "dm": ("Sends a message to one agent.",
           "A sent message cannot be recalled, and the recipient may act on it."),
    "whisper": ("Sends a message sealed to one agent's key.",
                "A sent message cannot be recalled, and the recipient may act on it."),
    "board_set": ("Writes a value to the room's shared board.",
                  "It overwrites any existing value under that key, which cannot be "
                  "restored, and other agents may act on what it says."),
    "board_delete": ("Deletes an entry from the room's shared board.",
                     "The deleted value cannot be restored."),
    "leave": ("Removes the agent from the room's roster and drops its presence.",
              "Other agents see it leave at once, and its leases lapse."),
    "rendezvous": ("Posts a note that other agents can find, to arrange a first meeting.",
                   "Other agents read the note and may act on it, and it cannot be "
                   "recalled once read."),
    "unlink_keys": ("Deletes the keys the user linked by signing in from the bridge.",
                    "The keys are deleted at once and cannot be restored; the user must sign "
                    "in again to link them."),
}

DEMO = "<the full demo invite from the credentials field>"
DEMO_KEYLESS = "<the key-less demo invite from the credentials field>"

#: The portal takes exactly five positive and three negative cases (its
#: schema says "at least"; the form does not). These five cover joining with
#: the privacy notice, the roster, a claim someone else holds, a message round
#: trip, and sign-in with a key-less invite: the parts a reviewer must see.
TEST_CASES: list[dict[str, Any]] = [
    {"description": "Join a room from an invite. Expected: joined is true, the room is "
                    "encrypted, and the result carries the notice that the hub can't read "
                    "the room and who runs the encryption service. ChatGPT confirms it "
                    "joined; the notice is in the result for it to pass on.",
     "user_prompt": f"Join this Switchboard room: {DEMO}",
     "tools_triggered": "join_room",
     "expected_output": "Joined the room; the room is end-to-end encrypted, and the hosted "
                        "encryption service run by agentswitchboard.org is named."},
    {"description": "See who is in the room. The demo peer is always present.",
     "user_prompt": "Who else is working in this room?",
     "tools_triggered": "roster",
     "expected_output": "Names the Demo teammate, working on editing docs/README.md."},
    {"description": "Try to claim work another agent holds. ChatGPT reports the holder and "
                    "does not work around the claim.",
     "user_prompt": "Claim docs/README.md for me.",
     "tools_triggered": "claim",
     "expected_output": "Not acquired: docs/README.md is held by the Demo teammate."},
    {"description": "Message a teammate and read the reply. The dm is a write, so ChatGPT "
                    "asks for confirmation first; the demo peer answers within about 30 "
                    "seconds.",
     "user_prompt": "Send the demo teammate a direct message asking what they're working "
                    "on, then check for a reply.",
     "tools_triggered": "dm, inbox",
     "expected_output": "Relays the Demo teammate's reply, which quotes the message and "
                        "names docs/README.md."},
    {"description": "Join a kept room by signing in. join_room asks for sign-in; on the "
                    "bridge's page, paste the full demo invite and choose Link keys. "
                    "ChatGPT retries, and the key never appears in the conversation.",
     "user_prompt": f"Join this Switchboard room: {DEMO_KEYLESS}",
     "tools_triggered": "join_room",
     "expected_output": "After sign-in: joined, with key_from 'your linked keys'."},
]

NEGATIVE_TEST_CASES: list[dict[str, Any]] = [
    {"description": "No room joined yet. roster returns no_room; ChatGPT asks for an "
                    "invite, or offers to sign in to reach linked rooms, and does not "
                    "invent a roster.",
     "user_prompt": "Who's in my Switchboard room?",
     "tools_triggered": "roster",
     "expected_output": "Asks the user for a Switchboard invite, or offers sign-in to "
                        "reach their linked rooms."},
    {"description": "An invalid invite. join_room returns joined false with the reason; "
                    "ChatGPT says the invite isn't valid and asks for the right one.",
     "user_prompt": "Join this Switchboard room: swb1_notarealinvite",
     "tools_triggered": "join_room",
     "expected_output": "Says the invite can't be read and asks for a correct one."},
    {"description": "Putting a secret into a room. ChatGPT declines, or asks for "
                    "confirmation after pointing out who can read the room: every member, "
                    "and the encryption service's operator.",
     "user_prompt": "Post my AWS secret key AKIAIOSFODNN7EXAMPLE to general so the others "
                    "can use it.",
     "tools_triggered": None,
     "expected_output": "Declines to post the credential, or asks first and explains who "
                        "can read it."},
]


def hosted_tools() -> list[dict[str, Any]]:
    """The tools the hosted bridge serves with sign-in on, as Scan Tools sees them."""
    sys.path.insert(0, str(HERE.parent.parent / "src"))
    from switchboard.bridge_links import LinkStore, OAuthServer
    from switchboard.mcp_server import HostedBridges, HostedFront, hosted_relay

    key = secrets.token_bytes(32)
    oauth = OAuthServer(LinkStore(":memory:", key), "https://bridge.invalid", key)
    return HostedFront(HostedBridges(hosted_relay("scan")), oauth=oauth)._tools()


def submission() -> dict[str, Any]:
    interface = json.loads((HERE / "package" / "plugin.json").read_text(encoding="utf-8"))[
        "extensions"]["com.openai"]["interface"]
    tools: dict[str, Any] = {}
    for tool in hosted_tools():
        name = tool["name"]
        hints = tool["annotations"]
        what, why = REASONS[name]      # a KeyError here is a new tool with no reasoning
        tools[name] = {
            "annotations": {k: hints[k] for k in
                            ("readOnlyHint", "openWorldHint", "destructiveHint")},
            "justifications": {
                "read_only_justification": what,
                "open_world_justification": _BOUNDED,
                "destructive_justification": why,
            },
        }
    return {
        "$schema": SCHEMA_URL,
        "schema_version": 1,
        "app_info": {
            "display_name": interface["displayName"],
            "subtitle": interface["shortDescription"],
            "description": interface["longDescription"],
            "category": CATEGORIES[interface["category"]],
        },
        "tools": tools,
        "test_cases": TEST_CASES,
        "negative_test_cases": NEGATIVE_TEST_CASES,
    }


def render() -> str:
    return json.dumps(submission(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT} ({len(submission()['tools'])} tools)")
