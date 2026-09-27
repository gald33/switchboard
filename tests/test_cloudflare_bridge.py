"""`scripts/cloudflare_bridge.py` against a fake Cloudflare API.

The script runs against production DNS for a zone that also serves the
website, from a session that cannot try it first. So what is held here is
what would hurt if it were wrong: it creates only what is missing, it never
rewrites rules that are not its own, a second run changes nothing, and a
dry run changes nothing at all.
"""

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "cloudflare_bridge", ROOT / "scripts" / "cloudflare_bridge.py")
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)

ORIGIN = "203.0.113.7"


class FakeCloudflare:
    """Just enough of the v4 API: two zones, their A records, and one
    entrypoint ruleset per phase. Records every write."""

    def __init__(self, *, bots: dict[str, Any] | None = None) -> None:
        self._ids = (f"id{n}" for n in itertools.count())
        self.zones = {"agentswitchboard.org": "zbridge", "lucille-ai.com": "zhub"}
        self.records: dict[str, list[dict[str, Any]]] = {
            "zbridge": [],
            "zhub": [{"id": "r-hub", "type": "A", "name": cf.HUB_HOSTNAME,
                      "content": ORIGIN, "proxied": True}],
        }
        self.rulesets: dict[tuple[str, str], dict[str, Any]] = {}
        self.bots = bots if bots is not None else {"fight_mode": False,
                                                   "ai_bots_protection": "disabled"}
        self.writes: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        if method != "GET":
            self.writes.append((method, path))
        parts = path.split("?")[0].strip("/").split("/")
        if path.startswith("/zones?name="):
            name = path.split("=", 1)[1]
            return [{"id": self.zones[name]}] if name in self.zones else []
        zone = parts[1]
        if parts[2] == "dns_records":
            if method == "GET":
                name = path.split("name=", 1)[1]
                return [r for r in self.records[zone] if r["name"] == name]
            if method == "POST":
                self.records[zone].append({"id": next(self._ids), **body})
                return {}
            if method == "PATCH":
                (record,) = [r for r in self.records[zone] if r["id"] == parts[3]]
                record.update(body)
                return record
        if parts[2] == "bot_management":
            return self.bots
        if parts[2] == "rulesets" and parts[3] == "phases":
            phase = parts[4]
            if method == "GET":
                return self.rulesets.get((zone, phase))
            ruleset = {"id": next(self._ids), "rules": [
                {"id": next(self._ids), **r} for r in body["rules"]]}
            self.rulesets[(zone, phase)] = ruleset
            return ruleset
        if parts[2] == "rulesets":
            ruleset = next(r for r in self.rulesets.values() if r["id"] == parts[3])
            if method == "POST":
                ruleset["rules"].append({"id": next(self._ids), **body})
                return ruleset
            if method == "PATCH":
                (rule,) = [r for r in ruleset["rules"] if r["id"] == parts[5]]
                rule.update(body)
                return ruleset
        raise AssertionError(f"unexpected call {method} {path}")


def run(api: FakeCloudflare, *, ip: str | None = None, dry: bool = False) -> list[str]:
    said: list[str] = []
    assert cf.run(api, ip, dry, said.append) == 0
    return said


def test_a_fresh_zone_gets_the_record_and_both_rules():
    api = FakeCloudflare()
    run(api)
    (record,) = api.records["zbridge"]
    assert (record["name"], record["content"], record["proxied"]) == (
        cf.HOSTNAME, ORIGIN, True)
    origin = api.rulesets[("zbridge", "http_request_origin")]["rules"]
    config = api.rulesets[("zbridge", "http_config_settings")]["rules"]
    assert origin[0]["action_parameters"] == {"origin": {"port": 8444}}
    assert config[0]["action_parameters"] == {"ssl": "strict"}
    assert origin[0]["expression"] == f'(http.host eq "{cf.HOSTNAME}")'


def test_a_second_run_changes_nothing():
    api = FakeCloudflare()
    run(api)
    api.writes.clear()
    said = run(api)
    assert api.writes == []
    assert sum("already in place" in line for line in said) == 3


def test_a_dry_run_changes_nothing():
    api = FakeCloudflare()
    said = run(api, dry=True)
    assert api.writes == []
    assert all(line.startswith(("[dry run]", "WARNING", "Cloudflare now"))
               for line in said)


def test_rules_that_are_not_ours_are_kept():
    # A PUT on an entrypoint replaces every rule in it; the website's own
    # origin rules must survive the bridge being added.
    api = FakeCloudflare()
    theirs = {"id": "theirs", "description": "website thing",
              "expression": '(http.host eq "agentswitchboard.org")', "action": "route",
              "action_parameters": {"origin": {"port": 3000}}}
    api.rulesets[("zbridge", "http_request_origin")] = {"id": "rs", "rules": [theirs]}
    run(api)
    rules = api.rulesets[("zbridge", "http_request_origin")]["rules"]
    assert rules[0] == theirs
    assert [r["description"] for r in rules] == ["website thing", cf.ORIGIN_RULE]
    assert ("PUT", "/zones/zbridge/rulesets/phases/http_request_origin/entrypoint") \
        not in api.writes


def test_a_record_left_unproxied_is_proxied():
    api = FakeCloudflare()
    api.records["zbridge"].append({"id": "r1", "type": "A", "name": cf.HOSTNAME,
                                   "content": ORIGIN, "proxied": False})
    run(api)
    assert api.records["zbridge"][0]["proxied"] is True


def test_an_explicit_origin_ip_wins():
    api = FakeCloudflare()
    run(api, ip="198.51.100.9")
    assert api.records["zbridge"][0]["content"] == "198.51.100.9"


def test_the_origin_ip_comes_from_the_hub_record():
    api = FakeCloudflare()
    api.records["zhub"].clear()
    with pytest.raises(cf.CloudflareError, match="--origin-ip"):
        run(api)


def test_bot_settings_that_would_block_chatgpt_are_reported_not_changed():
    api = FakeCloudflare(bots={"fight_mode": True, "ai_bots_protection": "block"})
    said = run(api)
    warnings = [line for line in said if line.startswith("WARNING")]
    assert len(warnings) == 2
    assert not any("bot_management" in path for _, path in api.writes)


def test_it_always_says_cloudflare_can_read_the_bridge():
    said = run(FakeCloudflare())
    assert "via Cloudflare" in said[-1]
