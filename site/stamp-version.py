#!/usr/bin/env python3
"""Stamp the released version into the landing page, from PyPI.

The number on the page is a promise about what `pip install` gives you, so it
is taken from the index that answers that command rather than typed by hand.
PyPI is the source of truth; `pyproject.toml` is cross-checked against it and
a mismatch is an error, because the two disagreeing is exactly the bug this
script exists to stop.

    python3 site/stamp-version.py            # check only
    python3 site/stamp-version.py --write    # check, then rewrite site/index.html

Run it at release time, after the new version is live on PyPI. It is not a
build step: it edits a committed file, and what is served stays what is in
the repo (site/PRINCIPLES.md, principle 11).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

PYPI = "https://pypi.org/pypi/agent-switchboard/json"
ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "site" / "index.html"

# Every place the page states the version. Both must end up agreeing.
MARKS = (
    re.compile(r'(<span class="ver">v)(\d+\.\d+\.\d+)(</span>)'),
    re.compile(r'(switchboard v)(\d+\.\d+\.\d+)( — MIT)'),
)


def published(timeout: float = 10.0) -> str:
    with urllib.request.urlopen(PYPI, timeout=timeout) as fh:
        return json.load(fh)["info"]["version"]


def declared() -> str:
    # Read the line rather than the TOML: tomllib is 3.11+, and this repo is
    # tested down to 3.10.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not found:
        raise SystemExit("no version = \"...\" line in pyproject.toml")
    return found.group(1)


def main(write: bool) -> int:
    try:
        live = published()
    except Exception as exc:                       # offline, or PyPI is down
        print(f"could not reach PyPI: {exc}", file=sys.stderr)
        return 2

    local = declared()
    if live != local:
        print(
            f"pyproject.toml says {local}, PyPI serves {live}.\n"
            "Release first, or bump the repo — do not stamp a version nobody "
            "can install.",
            file=sys.stderr,
        )
        return 1

    html = PAGE.read_text(encoding="utf-8")
    stale = [m.group(2) for pat in MARKS for m in pat.finditer(html) if m.group(2) != live]
    found = sum(len(pat.findall(html)) for pat in MARKS)

    if found != len(MARKS):
        print(f"expected {len(MARKS)} version marks in {PAGE.name}, found {found}", file=sys.stderr)
        return 1

    if not stale:
        print(f"{live} — PyPI, pyproject and the page all agree")
        return 0

    if not write:
        print(f"page says {sorted(set(stale))}, PyPI serves {live}; rerun with --write")
        return 1

    for pat in MARKS:
        html = pat.sub(lambda m: m.group(1) + live + m.group(3), html)
    PAGE.write_text(html, encoding="utf-8")
    print(f"stamped {live} into {PAGE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv[1:]))
