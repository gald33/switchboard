# The roadmap

The backlog as a graph of files, readable from a checkout with nothing
installed and no network. It is powered by
[`roadmap-core`](https://pypi.org/project/roadmap-core/) on its SQLite floor:
one file per item, one SQLite store that is derived and never committed, and
two generated markdown files that are.

Deliberately the same shape as everything else here — the record is the files,
the store is only the transaction that decides who holds what.

## The rules

**1. Authoring an item is writing a YAML file.** There is no `roadmap new`, and
that is on purpose: filing work belongs in a diff somebody reviews. Add
`roadmap/items/<key>.yaml`, then `roadmap push`.

**2. Every item owes evidence.** `validate` fails without it. Not a description
of the work — *why it is worth doing, and how you will know it worked*. Quote the
measurement, name the file and line, link the issue. An item whose evidence is a
restatement of its title tells the next session nothing it could not read from
the queue.

**3. Say where a claim came from.** Almost every item here is a filed GitHub
issue, and its `refs` carry the link. The one that is not
(`publish-hub-container-image`, read off a line in `README.md`) says so in its
first line of evidence. Keep that habit: an inferred item and a reported one
deserve different amounts of trust before somebody starts on it.

**4. `blocked_on` is for correctness, `related_to` is for everything else.**
`blocked_on` removes an item from the ready queue, so use it only when starting
the other one first is genuinely required. "Read this before touching that" is a
`related_to` with a note — and the note is not optional, because "figure out why
these are related" is the work the edge exists to prevent.

**5. The generated files are generated.** `roadmap/ROADMAP.md` and `ARCS.md` are
rebuilt wholesale by `sync`. Editing them by hand is lost work, and CI fails on
the drift.

## The commands

```bash
pip install "roadmap-core[files]"
export ROADMAP_SOURCE=local          # selects the SQLite store in this checkout

roadmap ready                        # what is startable, in priority order
roadmap show <key>                   # one item, with its evidence and edges
roadmap push                         # files -> store
roadmap validate                     # schema, dangling deps, cycles, missing evidence
roadmap sync                         # regenerate the two markdown files
roadmap claim <key>                  # take it; roadmap release <key> to drop it
roadmap status <key> done            # move the status deliberately
```

Without `ROADMAP_SOURCE=local` the CLI expects a served store and asks for a
credential that does not exist here. `.github/workflows/roadmap.yml` sets it for
CI; set it in your shell profile, or pass `--source local` per command.

## Three things that will look wrong

**`ARCS.md` lives at the repository root, not in `roadmap/`.** That path is fixed
in `roadmap-core` (`cli.py`'s `GENERATED_ARCS_MD`), and `sync --check` compares
against it. Moving it into `roadmap/` makes CI fail on a file it cannot find.

**The generated header says `python scripts/roadmap.py sync`.** There is no
`scripts/roadmap.py` in this repository. That instruction is inherited from the
project `roadmap-core` was extracted from; here the command is `roadmap sync`,
from the console script the package installs. Likewise the ARCS.md preamble links
three `docs/architecture/*.md` files that do not exist here.

**`roadmap/roadmap.db` is not committed**, and is already covered by the `*.db`
line in `.gitignore`. It is derived — `push` rebuilds it from the YAML on first
open — and a binary file in git would conflict on every single claim.

## What this is not

Not a replacement for GitHub issues, and not a mirror of them. An issue is where
a problem gets reported and argued; an item is where the project states what it
intends to do about it and in what order. Most items here point at an issue,
several issues will never become items, and the discussion stays on the issue.

Not coordination state either. Who is working on what *right now* is
Switchboard's job and expires on its own; a claim here is a durable statement
that outlives the session that made it. `docs/seam.md` is the long version of why
those are two different systems, and why this one may depend on Switchboard while
Switchboard never depends on it.
