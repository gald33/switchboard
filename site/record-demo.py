#!/usr/bin/env python3
"""Record demo/run.sh into an asciicast v2 file.

The landing page shows a real recording of the demo, not a dramatization
(site/PRINCIPLES.md, principle 3). This is how that recording is made, so
anyone can regenerate it and diff the result against what the page serves.

    python3 site/record-demo.py demo/run.sh site/demo.cast
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shlex
import signal
import struct
import sys
import termios
import time

COLS, ROWS = 100, 34


def record(argv: list[str], out_path: str) -> int:
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLUMNS"] = str(COLS)
        os.environ["LINES"] = str(ROWS)
        os.execvp(argv[0], argv)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    start = time.time()
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(json.dumps({
            "version": 2,
            "width": COLS,
            "height": ROWS,
            "timestamp": int(start),
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
            "title": " ".join(shlex.quote(a) for a in argv),
        }) + "\n")
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except InterruptedError:
                continue
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                out.write(json.dumps([
                    round(time.time() - start, 6), "o",
                    chunk.decode("utf-8", "replace"),
                ]) + "\n")
            else:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    return os.waitstatus_to_exitcode(status)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    script, out = sys.argv[1], sys.argv[2]
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(record(["bash", script], out))
