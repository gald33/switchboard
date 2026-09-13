#!/usr/bin/env python3
"""Check that every line the landing page quotes still exists in its source.

    python3 site/check-quotes.py

The page shows output from four recorded runs: `site/demo.cast`,
`site/clips/collision.txt`, `site/clips/crash.txt`, and two island boards that
live in another repository. Until 2026-09-12 the largest of those blocks was
*generated* from the cast by `site/build-terminal.py`, so drift was
impossible. That block is gone and the remaining quotes are hand-placed, which
means a change to the CLI's output can leave the page claiming a run printed
something it no longer prints. Principle 4 exists to prevent exactly that.

So this checks the local sources, and says which lines it could not find:

    ok   site/demo.cast            14 fragment(s)
    ok   site/clips/collision.txt   4 fragment(s)
    ...

What counts as a match, given the edits the page discloses under each block:

* **Whitespace is collapsed.** The page wraps long lines to fit a card, so one
  source line arrives as several fragments. Each fragment has to appear in the
  source; it does not have to appear as its own line.
* **`…` is an elision.** A fragment is split on it and each part must appear,
  in order, on the same source line. That covers shortened agent ids
  (`_e333FU6…`) and the two table columns the crash clip drops.
* **`$ ` and the script's own `# ` lines are ours**, not the run's, and are
  skipped — the recording's prompts were styled, and the capture scripts label
  their own steps.

Island lines are quoted from boards served by another host. `--online`
fetches and checks those too; without it they are listed as skipped, because a
CI run should not fail on somebody else's uptime.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SITE = Path(__file__).resolve().parent
PAGE = SITE / "index.html"

# Which block on the page is quoted from which file. The key is a marker that
# appears in that block's own fine print or heading, so a moved section keeps
# working and a renamed one fails loudly rather than silently checking nothing.
LOCAL_SOURCES = {
    "clips/collision.txt": SITE / "clips" / "collision.txt",
    "clips/crash.txt": SITE / "clips" / "crash.txt",
    "demo.cast": SITE / "demo.cast",
}

ONLINE_SOURCES = {
    "record.lucille-ai.com": "https://record.lucille-ai.com/games/board-ws_yrQ54GC3I5XUoRff2J9tKn.json",
    "ai-lab": "https://raw.githubusercontent.com/gald33/ai-lab/main/games/replays/board-island-game-002b-g1.json",
}

TAGS = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """Collapse whitespace so a wrapped quote matches an unwrapped source."""
    return SPACE.sub(" ", text).strip()


def cast_text(path: Path) -> list[str]:
    """Every line the recording printed, escape codes removed."""
    out: list[str] = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith('{"version"'):
            continue
        _, _, data = json.loads(raw)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", data).replace("\r", "")
        out.extend(line for line in clean.split("\n") if line.strip())
    return out


def board_text(payload: str) -> list[str]:
    """Every message body on an island board, one line each."""
    data = json.loads(payload)
    return [
        f"{m['at'][11:19]} {m['author']} {m['body']}"
        for m in data.get("messages", [])
    ]


def blocks(page: str) -> list[tuple[str, list[str]]]:
    """Each <pre> on the page, as (nearest source hint, fragments)."""
    found = []
    for match in re.finditer(r"<pre>(.*?)</pre>", page, re.S):
        body = match.group(1)
        # The links that follow a block name its source. Look to the end of
        # the enclosing section rather than a fixed window — the scene cites
        # its cast once, below both of its panes — and take the key that
        # appears *earliest*, not the first one this file happens to list.
        close = page.find("</section>", match.end())
        tail = page[match.end() : close if close > 0 else match.end() + 900]
        hits = [(tail.find(key), key)
                for key in list(LOCAL_SOURCES) + list(ONLINE_SOURCES)
                if key in tail]
        hint = min(hits)[1] if hits else None
        fragments = []
        for line in html.unescape(TAGS.sub("", body)).split("\n"):
            line = squash(line)
            if not line or line in {"$"}:
                continue
            if line.startswith("$ "):
                line = line[2:]           # the prompt is the page's, not the run's
            if line.startswith("# "):
                continue                  # the capture script's own labels
            fragments.append(line)
        found.append((hint, fragments))
    return found


def appears(fragment: str, lines: list[str]) -> bool:
    """Is this fragment somewhere in the source, allowing marked elisions?

    Two passes. An elided fragment (`PQVzwirn3q…  57s`) has to match inside a
    single source line, because an ellipsis that could span lines would let
    any two unrelated strings pass. Everything else may also match the source
    run as one flowing text, since the page wraps at a card's width and the
    terminal wrapped at its own — a sentence can straddle a line break in one
    and not the other.
    """
    parts = [squash(p) for p in fragment.split("…")]

    for line in lines:
        flat = squash(line)
        at = 0
        for part in parts:
            if not part:
                continue
            found = flat.find(part, at)
            if found < 0:
                break
            at = found + len(part)
        else:
            return True

    if len(parts) == 1:
        return parts[0] in squash(" ".join(lines))
    return False


def fetch(url: str) -> str:
    # Say who is calling. The island's record host refuses the default
    # `Python-urllib/x.y` agent outright (403), which looks exactly like a
    # dead link until you send something else.
    request = urllib.request.Request(url, headers={
        "User-Agent": "switchboard-site-check-quotes (+https://github.com/gald33/switchboard)",
    })
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true",
                        help="also check the island boards, which are served elsewhere")
    args = parser.parse_args()

    page = PAGE.read_text()
    sources: dict[str, list[str]] = {}
    for key, path in LOCAL_SOURCES.items():
        if not path.exists():
            print(f"missing source: {path}", file=sys.stderr)
            return 2
        sources[key] = (cast_text(path) if path.suffix == ".cast"
                        else path.read_text().splitlines())
    if args.online:
        for key, url in ONLINE_SOURCES.items():
            try:
                sources[key] = board_text(fetch(url))
            except (urllib.error.URLError, ValueError) as exc:
                # Somebody else's host, so this is not a failed check — say
                # what happened and leave those fragments unchecked.
                print(f"could not read {key}: {exc}", file=sys.stderr)

    checked: dict[str, int] = {}
    skipped: dict[str, int] = {}
    bad: list[tuple[str, str]] = []

    for hint, fragments in blocks(page):
        if hint is None:
            bad.append(("(no source link after the block)", fragments[0] if fragments else ""))
            continue
        if hint not in sources:
            skipped[hint] = skipped.get(hint, 0) + len(fragments)
            continue
        for fragment in fragments:
            checked[hint] = checked.get(hint, 0) + 1
            if not appears(fragment, sources[hint]):
                bad.append((hint, fragment))

    for key in sorted(checked):
        print(f"ok   {key:26} {checked[key]:3} fragment(s)")
    for key in sorted(skipped):
        print(f"--   {key:26} {skipped[key]:3} fragment(s) — pass --online to check")

    if bad:
        print("\nnot found in the source it cites:", file=sys.stderr)
        for key, fragment in bad:
            print(f"  {key}: {fragment}", file=sys.stderr)
        print("\nRe-capture the run, or fix the quote. The page may not show a line"
              "\nthat no longer exists — site/PRINCIPLES.md, principle 4.", file=sys.stderr)
        return 1

    print("\nevery quoted line is in the run it cites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
