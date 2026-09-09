#!/usr/bin/env python3
"""Render site/demo.cast into the HTML block the landing page ships.

The page's terminal is the recording, not a retyping of it: this reads the
asciicast, splits it into lines, and writes each line with the real timestamp
at which it finished appearing. So the transcript is byte-accurate and the
playback rhythm is the rhythm of the actual run.

    python3 site/build-terminal.py site/demo.cast site/terminal.html

Paste the result between the TERMINAL markers in site/index.html.
"""
from __future__ import annotations

import html
import json
import re
import sys

# The only SGR codes demo/run.sh emits.
CLASS = {"1": "b", "2": "d", "33": "w", "0": None, "": None}
ANSI = re.compile(r"\x1b\[([0-9;]*)m")


def spans(line: str) -> str:
    """One transcript line as HTML, honouring the recorded colours."""
    out, cls, pos = [], None, 0
    for m in ANSI.finditer(line):
        text = line[pos:m.start()]
        if text:
            esc = html.escape(text)
            out.append(f'<i class="{cls}">{esc}</i>' if cls else esc)
        cls = CLASS.get(m.group(1).split(";")[0], None)
        pos = m.end()
    tail = line[pos:]
    if tail:
        esc = html.escape(tail)
        out.append(f'<i class="{cls}">{esc}</i>' if cls else esc)
    return "".join(out) or "&nbsp;"


def build(cast_path: str) -> tuple[str, float]:
    raw = open(cast_path, encoding="utf-8").read().splitlines()[1:]
    frames = [json.loads(row) for row in raw]
    lines: list[tuple[float, str]] = []
    buf = ""
    for when, kind, data in frames:
        if kind != "o":
            continue
        buf += data.replace("\r\n", "\n")
        while "\n" in buf:
            head, buf = buf.split("\n", 1)
            lines.append((when, head))
    if buf:
        lines.append((frames[-1][0], buf))

    total = frames[-1][0]
    rows = [
        f'<span class="l" data-t="{when:.2f}">{spans(text)}</span>'
        for when, text in lines
    ]
    return "\n".join(rows), total


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    body, total = build(sys.argv[1])
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    print(f"{len(body.splitlines())} lines, {total:.1f}s -> {sys.argv[2]}")
