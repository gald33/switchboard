#!/usr/bin/env python3
"""Zip the plugin package for OpenAI's plugin portal.

The portal takes one ZIP with the plugin at its root: `plugin.json` (the
listing, under `extensions.com.openai.interface`), `mcp.json`, `skills/` and
`assets/`. `mcp.json` declares the hosted bridge. The listing was created
With MCP, and the portal refuses an update that omits that server
("Removing the MCP isn't supported"). `.app.json` is the opposite case: ZIP
uploads must not carry one, and `.mcp.json` is derived from `mcp.json` on
ingestion, so neither belongs in the package.

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
#: What the package must carry, and what it must not; see the module docstring.
REQUIRED = {"plugin.json", "mcp.json"}
EXCLUDED = {".mcp.json", ".app.json"}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def files(root: Path = PACKAGE) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def build(out: Path = DEFAULT_OUT, root: Path = PACKAGE) -> Path:
    included = files(root)
    # Checked at the root rather than among `included`: `files` skips dotfiles,
    # so a stray one would otherwise be left out without a word.
    stray = sorted(name for name in EXCLUDED if (root / name).exists())
    if stray:
        raise SystemExit(f"{', '.join(stray)} must not be in the package: the portal "
                         "takes the MCP server from mcp.json")
    missing = sorted(REQUIRED - {p.name for p in included if p.parent == root})
    if missing:
        raise SystemExit(f"the package needs {', '.join(missing)} at its root")
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
