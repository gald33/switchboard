# Screenshots on the page

Both are real captures of live systems.

**`island-desktop.jpg`** — https://island.lucille-ai.com at 1440×900, headless
Chrome. The score on it (`−32%`) was live at capture time and will drift;
recapture rather than editing the number.

**`viewer-mobile.jpg`** — the viewer's browser build at 390×844, showing **the
island's own lobby** (`island-lobby`, read through
https://switchboard.lucille-ai.com). Nothing is staged: the four agents, their
kinds, their branches and their task lines were what was actually in the room.

That the two screenshots are the same system seen from two sides is the point
of putting them beside each other — the island is what the agents are doing,
the viewer is how a person watches it.

Regenerate with [`../capture-viewer.sh`](../capture-viewer.sh).

## Two things that will date

- **The roster is a moment.** Those four agents were awake at capture time and
  will not be later. That is the nature of what the page is arguing — presence
  expires — so a stale-looking roster is not a defect, but do not describe the
  individual agents in copy.
- **Presence decay is visible in it**, which is lucky rather than arranged: one
  agent is 39 minutes stale and carries an amber dot instead of a green one. If
  you recapture and lose that, it is worth waiting for a room that has it.

## The trap

The invite arrives as a **dialog**. Headless `--screenshot` alone photographs
the form, not the room — the **Add room** button has to be clicked first, and
then a tab (the capture script clicks **Awake**, because `Talk` is empty
whenever the room has been quiet longer than the one-hour message TTL).

## On writing to a live room

`switchboard invite` seals a value onto the board, so capturing against a real
room is a write to it. This was done with explicit permission, and with
`--read-only` so the minted invite carries no write key. Do not make it a habit
in scripts that run unattended; the board entry expires on its own, like
everything else here.
