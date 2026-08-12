"""The coordination protocol, as text, for whoever needs to read it.

`init` installs the packaged `SKILL.md` into a repo, which serves the agent
whose harness loads skills from disk. Not every agent has one: an MCP client
with no skill mechanism, a CLI session in a repo nobody ran `init` in, or a
session that has hit something confusing and needs the convention *now*
rather than after a human wires it up. Those callers get the same text from
the `help` tool and `switchboard help`.

The point of putting the accessor here rather than in `cli.py` is that
`mcp_server.py` must not import the CLI to read a file: the bridge deliberately
carries no dependency it does not need, and the CLI pulls in the whole init
machinery. One reader, imported by both.
"""

from __future__ import annotations

from importlib import resources

#: The directory name under `skill/`, which is also the skill's name and the
#: directory `init` installs it into.
SKILL_NAME = "switchboard-coordinate"


def skill_text() -> str:
    """The packaged coordination protocol — the single source `init` installs
    from, `help` serves, and the docs link to, so there is only ever one copy
    of this protocol to drift out of sync."""
    return (
        resources.files("switchboard")
        .joinpath("skill", SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )


def skill_history() -> list[str]:
    """Every SKILL.md `init` has ever installed, oldest first, excluding the
    current one.

    Kept as files rather than string literals because the skill is a few
    hundred lines of prose: a past revision is copied out of git verbatim
    (`git show <rev>:...SKILL.md > history/00N-SKILL.md`), so it cannot drift
    from what actually shipped the way a retyped literal can. Ordering is by
    filename, which is why they are zero-padded.

    Revisions 001 and 002 were backfilled rather than recorded as they
    shipped, so a repo initialised before this existed would have had its
    SKILL.md read as hand-edited and left frozen at whatever `init` wrote.
    """
    directory = resources.files("switchboard").joinpath("skill", "history")
    return [
        f.read_text(encoding="utf-8")
        for f in sorted(directory.iterdir(), key=lambda f: f.name)
        if f.name.endswith(".md")
    ]
