# Screenshots on the page

Both are real captures of live systems.

Both sit inside a device drawn in CSS on the page — a phone for the viewer, a
laptop for the game — so keep their aspect ratios or they will be cropped by
the frame.

**`island-play.jpg`** — actual gameplay, not the marketing page: a replay of
`005-ladder-b-l-both-seed5` seeked to day 4 of 5, captured at 1280×800 with a
2× device scale. Trades are mid-flight in the bubbles over the island and the
counters read settled 12 / refused 2 / lapsed 11. Load it with
`?board=…&reveal=…` and move the range input; day 1 shows an empty board and
all-zero utilities, which is a dull picture of a game about trade.

**`viewer-phone.jpg`** — the viewer's browser build at 390×760, 3× scale,
showing **the island's own lobby** (`island-lobby`, read through
https://switchboard.lucille-ai.com). Nothing is staged: the agents, their
kinds, branches and task lines were what was actually in the room.

Keep this one **uncropped**. It is 9:17.5, which is what the phone frame
expects; a shorter crop gets its sides eaten by `object-fit: cover`, which is
how the wordmark once rendered as "oard".

That the two screenshots are the same system seen from two sides is the point
of putting them beside each other — the island is what the agents are doing,
the viewer is how a person watches it.

Regenerate with [`../capture-viewer.sh`](../capture-viewer.sh).

## Two things that will date

- **The roster is a moment.** Those agents were awake at capture time and will
  not be later. That is the nature of what the page is arguing — presence
  expires — so a stale-looking roster is not a defect, but do not describe the
  individual agents in copy.
- **Presence decay is visible in it**, which is lucky rather than arranged: one
  agent has gone quiet and carries an orange dot instead of a green one. If you
  recapture and lose that, it is worth waiting for a room that has it. The
  caption deliberately does not name the number of minutes — it changed between
  two captures and made the copy wrong.

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
