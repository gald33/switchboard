# Screenshots on the page

Both are real captures, not mockups.

**`island-desktop.jpg`** — https://island.lucille-ai.com at 1440×900, captured
with headless Chrome. The score on it (`−32%`) was live at capture time and
will drift; recapture rather than editing the number.

**`viewer-mobile.jpg`** — the viewer's browser build at 390×844.

Captured against a **throwaway local hub**, deliberately, rather than against
the live island room. Pointing the viewer at the real room would have meant
minting an invite, and `switchboard invite` seals a value onto the board — a
write to a live game. A screenshot is not worth a side effect on somebody's
running match.

So the room in the shot is one built for the shot: three agents on `build`,
two claims, one board entry. The viewer code, the encryption, the `verified`
badge and the read-only footer are all the real thing; only the room is
staged, and it is staged the way `demo/run.sh` stages its hub.

To recapture: boot a hub with `--cors-origin` pointing at wherever you serve
`extras/viewer/switchboard_viewer/web`, populate a room, then
`switchboard invite --link <that page>` and drive the resulting URL with
Playwright — the invite arrives as a dialog and needs the **Add room** button
clicked before the room renders.
