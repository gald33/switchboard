#!/usr/bin/env python3
"""Zip the plugin package for OpenAI's plugin portal.

The portal takes one ZIP with the plugin at its root: `plugin.json` (the
listing, under `extensions.com.openai.interface`), `skills/` and `assets/`.
The MCP server is not in it: a remote server is entered in the portal's
**With MCP** form, and a package that bundles `mcp.json` or `.app.json` is
refused for that reason.

    python3 extras/chatgpt-plugin/build_package.py            # -> switchboard-plugin.zip
    python3 extras/chatgpt-plugin/build_package.py out.zip

Entries are written in sorted order with a fixed timestamp, so the same tree
always makes the same bytes.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent / "package"
DEFAULT_OUT = Path(__file__).resolve().parent / "switchboard-plugin.zip"
#: What a With-MCP package must not carry; see the module docstring.
EXCLUDED = {"mcp.json", ".mcp.json", ".app.json"}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def files(root: Path = PACKAGE) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def build(out: Path = DEFAULT_OUT, root: Path = PACKAGE) -> Path:
    included = files(root)
    stray = [p.name for p in included if p.name in EXCLUDED]
    if stray:
        raise SystemExit(f"{', '.join(stray)} must not be in the package: the MCP "
                         "server goes in the portal's With MCP form")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in included:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return out


if __name__ == "__main__":
    target = build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
    print(f"wrote {target} ({len(files())} files)")
