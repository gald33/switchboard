"""What this repo has told Switchboard about how its agents work together.

Switchboard enumerates nothing. It does not know what a roadmap is, what an
arc is, or that "orchestrator" and "implementer" are different jobs — and it
must not, because the hub cannot read a payload and so could never enforce a
meaning it held an opinion about. But agents coordinating in a repo *do* need
those answers, and re-deriving them per session is how two sessions end up
holding different ones.

So the repo answers, and this is where the answer is written down. Switchboard
supplies the mechanism — where the file lives, how staleness is decided, what
a refresh has to record — and the agent supplies every value in it. A field
here is content Switchboard carries and never interprets, exactly as
``execution_class`` is a free-form label rather than a fixed list.

**Why the repo rather than the blackboard.** This is a fact about the
codebase, so it belongs where facts about the codebase live: committed,
versioned with the code that made it true, reviewable in the diff, and present
in a fresh clone before any hub is reachable. On the blackboard it would be
gone in twenty-four hours and unreadable to anyone on another key.

**Provenance is not decoration.** A value a human asserted must never be
silently replaced by one an agent inferred. `init` already draws this line for
the files it writes — untouched output is upgraded, hand-edited output is left
alone unless forced — and inference is exactly the case that needs it, because
a confidently wrong spec is worse than an absent one: every later agent reads
it and none of them doubt it.

**Staleness is a comparison, not a judgement.** A refresh records a
fingerprint of the inputs the spec was derived from, so the next agent asks
"did those files change?" rather than "does this feel old?". The history is
the fallback for inputs that cannot be fingerprinted, and is why the outcome
vocabulary keeps `unchanged` and `failed` apart — collapsing "I checked and
nothing moved" into "I could not tell" sends the next investigation to the
wrong place, the same mistake the drill verdicts exist to avoid.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Committed, like `rooms.json` beside it. Non-secret by construction: it holds
#: conventions, not credentials.
SPEC_FILE = ".switchboard/spec.json"

SPEC_VERSION = 1

#: How many refresh records to keep. Enough to see a cadence, few enough that
#: the file stays reviewable in a diff.
HISTORY_LIMIT = 20

#: What a refresh is allowed to have concluded.
#:
#: `unchanged` and `failed` are deliberately not one outcome. The first says
#: the spec is fresh and the next agent can skip the work; the second says a
#: field is a known unknown and re-attempting it blindly will fail the same
#: way. Collapsed together they read identically and every later agent pays
#: the same cost to rediscover it.
OUTCOMES = ("unchanged", "updated", "partial", "failed")

#: Who put a value here. `asserted` outranks `inferred` and is never
#: overwritten by it without `force`.
PROVENANCES = ("asserted", "inferred")


class SpecError(Exception):
    """A spec file that cannot be read, or a write that would lose something."""


@dataclass
class Field:
    value: Any
    provenance: str = "inferred"
    #: Free text: where the agent got this. Not parsed, only shown — the next
    #: agent deciding whether to trust a value wants the reasoning, and the
    #: reasoning is not something Switchboard can generate for it.
    evidence: str = ""

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "provenance": self.provenance}
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass
class Refresh:
    at: float
    outcome: str
    note: str = ""
    #: The fingerprint at the time, so a later reader can tell whether this
    #: record still describes the repo in front of it.
    fingerprint: str = ""
    by: str = ""

    def as_json(self) -> dict[str, Any]:
        out = {"at": self.at, "outcome": self.outcome}
        if self.note:
            out["note"] = self.note
        if self.fingerprint:
            out["fingerprint"] = self.fingerprint
        if self.by:
            out["by"] = self.by
        return out


@dataclass
class Spec:
    """The whole file: what is known, what it was derived from, and when."""

    fields: dict[str, Field] = field(default_factory=dict)
    #: Repo-relative paths the spec was derived from. Declared by whoever ran
    #: the refresh, because only they know what they read.
    inputs: list[str] = field(default_factory=list)
    history: list[Refresh] = field(default_factory=list)

    # --- staleness ---------------------------------------------------------

    def fingerprint(self, root: Path) -> str:
        """A hash over the declared inputs, as they are on disk right now.

        Content rather than mtime: a checkout, a rebase and a branch switch all
        move mtimes without changing a byte, and an agent that refreshed on
        every one of those would learn nothing and cost a turn each time.

        A missing input is folded in as such rather than skipped, so deleting a
        file the spec was built from reads as a change instead of as agreement.
        """
        if not self.inputs:
            return ""
        digest = hashlib.sha256()
        for rel in sorted(self.inputs):
            path = root / rel
            digest.update(rel.encode("utf-8", "replace"))
            digest.update(b"\x00")
            try:
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                digest.update(b"<absent>")
            digest.update(b"\x00")
        return "sha256:" + digest.hexdigest()[:32]

    @property
    def last(self) -> Refresh | None:
        return self.history[-1] if self.history else None

    def is_stale(self, root: Path) -> bool | None:
        """True/False when it can be decided, None when it cannot.

        None is a real answer and not a failure: with no inputs declared there
        is nothing to compare, so the honest report is "I cannot tell from
        here, look at the cadence" rather than a confident False that would
        stop anyone ever refreshing again.
        """
        if not self.inputs or self.last is None or not self.last.fingerprint:
            return None
        return self.fingerprint(root) != self.last.fingerprint

    # --- serialisation -----------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> Spec:
        path = root / SPEC_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise SpecError(f"{path} is not valid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise SpecError(f"{path} should hold an object")
        fields = {}
        for name, raw in (data.get("fields") or {}).items():
            if not isinstance(raw, dict) or "value" not in raw:
                raise SpecError(
                    f"{path}: field {name!r} must be an object with a 'value'"
                )
            provenance = str(raw.get("provenance") or "inferred")
            if provenance not in PROVENANCES:
                raise SpecError(
                    f"{path}: field {name!r} has provenance {provenance!r}; "
                    f"expected one of {', '.join(PROVENANCES)}"
                )
            fields[name] = Field(
                value=raw["value"], provenance=provenance,
                evidence=str(raw.get("evidence") or ""),
            )
        history = [
            Refresh(
                at=float(entry.get("at") or 0.0),
                outcome=str(entry.get("outcome") or "updated"),
                note=str(entry.get("note") or ""),
                fingerprint=str(entry.get("fingerprint") or ""),
                by=str(entry.get("by") or ""),
            )
            for entry in (data.get("history") or [])
            if isinstance(entry, dict)
        ]
        inputs = [str(p) for p in (data.get("inputs") or []) if isinstance(p, str)]
        return cls(fields=fields, inputs=inputs, history=history)

    def save(self, root: Path) -> Path:
        path = root / SPEC_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SPEC_VERSION,
            "inputs": sorted(self.inputs),
            "fields": {n: f.as_json() for n, f in sorted(self.fields.items())},
            "history": [r.as_json() for r in self.history[-HISTORY_LIMIT:]],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
        return path

    # --- writing -----------------------------------------------------------

    def apply(self, updates: dict[str, Field], *, force: bool = False) -> list[str]:
        """Merge `updates` in. Returns the names actually changed.

        An inferred value never silently replaces an asserted one. That is the
        whole reason provenance is recorded, and the rule is the same one
        `init` applies to hand-edited files — with `force` as the same escape
        hatch, so the answer to "I really do mean it" is a flag rather than
        editing the file by hand and losing the provenance too.
        """
        changed = []
        for name, incoming in updates.items():
            existing = self.fields.get(name)
            if (existing is not None and not force
                    and existing.provenance == "asserted"
                    and incoming.provenance == "inferred"):
                continue
            if existing is not None and existing.as_json() == incoming.as_json():
                continue
            self.fields[name] = incoming
            changed.append(name)
        return sorted(changed)

    def record(self, refresh: Refresh) -> None:
        if refresh.outcome not in OUTCOMES:
            raise SpecError(
                f"outcome {refresh.outcome!r} is not one of {', '.join(OUTCOMES)}"
            )
        self.history.append(refresh)
        del self.history[:-HISTORY_LIMIT]


def roles_for(root: Path) -> dict[str, Any]:
    """Role overlays this repo declares, or an empty mapping.

    Here rather than in `cli.py` for the same reason `guidance.skill_text` is:
    the MCP bridge must not import the CLI to read a file — it deliberately
    carries no dependency it does not need, and the CLI pulls in the whole init
    machinery. One reader, imported by both.

    Switchboard ships no roles and no overlay text. What an orchestrator *is*
    belongs to whatever system decomposes the work, and the moment this package
    defines one it has taken an opinion it could never enforce, since the hub
    cannot read a payload to check the claim.
    """
    try:
        roles = Spec.load(root).fields.get("roles")
    except SpecError:
        return {}
    value = roles.value if roles else None
    return value if isinstance(value, dict) else {}


def cadence(history: list[Refresh]) -> float | None:
    """Mean seconds between refreshes that found something, or None.

    Only `updated` and `partial` count. Runs that found nothing say how often
    somebody *looked*, which is a fact about the agents; the question this
    answers is how often the repo actually *moves*, which is a fact about the
    repo and the only one worth pacing against.
    """
    # `> 0` rather than truthiness, and stated rather than implied: a record
    # loaded without a timestamp defaults to 0.0, so that is the "unknown" it
    # has to exclude — but writing it as `and r.at` also silently discards a
    # genuine epoch-0 time, which is the sort of thing that only shows up in a
    # test fixture and then looks like the test being wrong.
    moved = [r.at for r in history if r.outcome in ("updated", "partial") and r.at > 0]
    if len(moved) < 2:
        return None
    return (moved[-1] - moved[0]) / (len(moved) - 1)


def now() -> float:
    return time.time()
