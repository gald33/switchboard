#!/usr/bin/env python3
"""Put the hosted bridge on bridge.agentswitchboard.org, through Cloudflare.

The same route the hub already takes to lucille-vm (docs/deployment.md,
"Sharing a VM with another app"): Cloudflare answers on :443, an Origin Rule
sends the request to the switchboard-only Caddy on :8444, and a Configuration
Rule holds that hop to Full (strict), which the DNS-01 certificate Caddy
already issues is what makes possible. Three things, each created only if it
is missing, so running this twice changes nothing the second time:

1. a proxied `A` record for the bridge's hostname, at the hub's origin IP
2. an Origin Rule: that hostname -> port 8444
3. a Configuration Rule: that hostname -> SSL Full (strict)

Scoped to the one hostname throughout. Nothing zone-wide is changed — not the
SSL mode, not bot settings — because the zone also serves the website. What
this does *check* zone-wide is the bot protection, since ChatGPT connects from
OpenAI's servers and a challenge page is what it would get instead of MCP.

Proxying means Cloudflare terminates TLS, so it sees what the bridge sees:
plaintext, and the invite in each URL. Say so in SWITCHBOARD_BRIDGE_OPERATOR
(docs/chatgpt.md). This script prints the reminder; it cannot enforce it.

Usage, with a token scoped as docs/deployment.md describes:

    CLOUDFLARE_API_TOKEN=... python3 scripts/cloudflare_bridge.py --dry-run
    CLOUDFLARE_API_TOKEN=... python3 scripts/cloudflare_bridge.py

The origin IP is read from the hub's own DNS record unless --origin-ip is
given. Standard library only, so it runs on the VM or anywhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

API = "https://api.cloudflare.com/client/v4"

HOSTNAME = "bridge.agentswitchboard.org"
ZONE = "agentswitchboard.org"
#: Where the origin IP comes from: the hub's record, which already points at
#: lucille-vm. Read rather than copied here, so the bridge cannot drift onto a
#: stale address when the VM moves.
HUB_HOSTNAME = "switchboard.lucille-ai.com"
HUB_ZONE = "lucille-ai.com"
ORIGIN_PORT = 8444

ORIGIN_RULE = f"switchboard bridge: {HOSTNAME} -> :{ORIGIN_PORT}"
CONFIG_RULE = f"switchboard bridge: {HOSTNAME} -> SSL Full (strict)"

Request = Callable[[str, str, Any], Any]


class CloudflareError(RuntimeError):
    pass


def http_request(token: str) -> Request:
    """A caller for the v4 API that returns `result`, or raises with the
    API's own error messages — which name the missing permission, the one
    thing a person fixing a token needs to read."""

    def call(method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except ValueError:
                raise CloudflareError(f"{method} {path}: HTTP {exc.code}") from exc
            if exc.code == 404:
                return None
        if not payload.get("success", False):
            errors = "; ".join(e.get("message", str(e)) for e in payload.get("errors", []))
            raise CloudflareError(f"{method} {path}: {errors or 'failed'}")
        return payload.get("result")

    return call


def zone_id(call: Request, name: str) -> str:
    zones = call("GET", f"/zones?name={name}", None) or []
    if not zones:
        raise CloudflareError(
            f"zone {name!r} is not visible to this token — give it Zone:Read on {name}")
    return zones[0]["id"]


def a_records(call: Request, zone: str, name: str) -> list[dict[str, Any]]:
    return call("GET", f"/zones/{zone}/dns_records?type=A&name={name}", None) or []


def origin_ip(call: Request) -> str:
    records = a_records(call, zone_id(call, HUB_ZONE), HUB_HOSTNAME)
    if not records:
        raise CloudflareError(
            f"no A record for {HUB_HOSTNAME} — pass --origin-ip, or give the token "
            f"DNS:Read on {HUB_ZONE}")
    return records[0]["content"]


def ensure_record(call: Request, zone: str, ip: str, dry: bool) -> str:
    existing = a_records(call, zone, HOSTNAME)
    want = {"type": "A", "name": HOSTNAME, "content": ip, "proxied": True, "ttl": 1,
            "comment": "switchboard hosted bridge (lucille-vm)"}
    if not existing:
        if not dry:
            call("POST", f"/zones/{zone}/dns_records", want)
        return f"created A {HOSTNAME} -> {ip} (proxied)"
    record = existing[0]
    if record["content"] == ip and record.get("proxied"):
        return f"A {HOSTNAME} -> {ip} (proxied) already in place"
    if not dry:
        call("PATCH", f"/zones/{zone}/dns_records/{record['id']}",
             {"content": ip, "proxied": True})
    return (f"updated A {HOSTNAME}: {record['content']} "
            f"(proxied={record.get('proxied')}) -> {ip} (proxied)")


def ensure_rule(call: Request, zone: str, phase: str, rule: dict[str, Any],
                dry: bool) -> str:
    """Add `rule` to the zone's entrypoint ruleset for `phase`, unless a rule
    with its description is already there.

    Added with the per-rule endpoint rather than by rewriting the entrypoint:
    PUT on an entrypoint replaces every rule in it, and this zone's other
    rules are not this script's to rewrite.
    """
    entry = call("GET", f"/zones/{zone}/rulesets/phases/{phase}/entrypoint", None)
    if entry is None:
        if not dry:
            call("PUT", f"/zones/{zone}/rulesets/phases/{phase}/entrypoint",
                 {"rules": [rule]})
        return f"created {phase} with: {rule['description']}"
    for existing in entry.get("rules") or []:
        if existing.get("description") == rule["description"]:
            same = (existing.get("expression") == rule["expression"]
                    and existing.get("action_parameters") == rule["action_parameters"]
                    and existing.get("enabled", True))
            if same:
                return f"{rule['description']}: already in place"
            if not dry:
                call("PATCH", f"/zones/{zone}/rulesets/{entry['id']}/rules/{existing['id']}",
                     rule)
            return f"{rule['description']}: updated"
    if not dry:
        call("POST", f"/zones/{zone}/rulesets/{entry['id']}/rules", rule)
    return f"{rule['description']}: added"


def rules() -> list[tuple[str, dict[str, Any]]]:
    expression = f'(http.host eq "{HOSTNAME}")'
    return [
        ("http_request_origin", {
            "description": ORIGIN_RULE, "expression": expression, "action": "route",
            "action_parameters": {"origin": {"port": ORIGIN_PORT}}, "enabled": True,
        }),
        ("http_config_settings", {
            "description": CONFIG_RULE, "expression": expression, "action": "set_config",
            "action_parameters": {"ssl": "strict"}, "enabled": True,
        }),
    ]


def bot_warnings(call: Request, zone: str) -> list[str]:
    """Zone-wide bot settings that would answer ChatGPT with a challenge.
    Reported, never changed: they are the website's too."""
    try:
        bots = call("GET", f"/zones/{zone}/bot_management", None) or {}
    except CloudflareError as exc:
        return [f"could not read bot settings ({exc}); check Bot Fight Mode and "
                "'Block AI bots' by hand"]
    out = []
    if bots.get("fight_mode"):
        out.append("Bot Fight Mode is ON for the zone — it can challenge ChatGPT's "
                   "requests, and cannot be skipped per hostname on this plan")
    if bots.get("ai_bots_protection") not in (None, "disabled"):
        out.append(f"AI bot blocking is {bots['ai_bots_protection']!r} — it can block "
                   "OpenAI's servers from reaching the bridge")
    return out


def run(call: Request, ip: str | None, dry: bool, say: Callable[[str], None]) -> int:
    zone = zone_id(call, ZONE)
    ip = ip or origin_ip(call)
    prefix = "[dry run] " if dry else ""
    say(prefix + ensure_record(call, zone, ip, dry))
    for phase, rule in rules():
        say(prefix + ensure_rule(call, zone, phase, rule, dry))
    for warning in bot_warnings(call, zone):
        say("WARNING: " + warning)
    say(f"Cloudflare now terminates TLS for {HOSTNAME}, so it can read what the bridge "
        "reads. Name it in SWITCHBOARD_BRIDGE_OPERATOR, e.g. "
        '"agentswitchboard.org (via Cloudflare)".')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="read everything, change nothing, say what would change")
    parser.add_argument("--origin-ip",
                        help=f"the VM's public IP (default: read from {HUB_HOSTNAME})")
    args = parser.parse_args(argv)
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        print("set CLOUDFLARE_API_TOKEN (see docs/deployment.md for its scope)",
              file=sys.stderr)
        return 2
    try:
        return run(http_request(token), args.origin_ip, args.dry_run, print)
    except CloudflareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
