# Brief for Claude Design — agentswitchboard.org

Paste this whole file into Claude Design. It is written to be complete on its
own: Claude Design cannot see this repository, so every fact, command and line
of copy it needs is reproduced here verbatim. Nothing in it should be invented,
improved or padded.

The starting artboards are `Main.dc.html` (desktop, 1200px) and
`Mobile.dc.html` (390px) beside this file — plain HTML, inline styles, no
framework. Strip the `<x-dc>` wrapper and the `support.js` head line and each
is an ordinary page.

---

## 1. What this is

A single-page landing site for **Switchboard**, an ephemeral coordination hub
for AI coding agents. Open source, MIT, self-hostable, currently at v2.3.0.

## 2. The one reader

An engineer who **already runs two or more coding agents on one repo and has
felt them collide** — two sessions editing the same migration, a stale claim
nobody released, a question asked into an inbox nothing was watching. They
arrive from a README link or a colleague. They are not shopping for a
category. They are deciding whether this is real and whether it is five
minutes or five days of work. They will paste a command within a minute of
landing.

Everyone else is a bystander. Do not widen the copy to include investors, AI
tourists, or people who have never run two agents. The page loses more by
sounding generic to the one reader than it gains by sounding legible to the
rest.

## 3. Voice

Closer to a well-made protocol spec than a SaaS homepage. Plain, specific,
unhurried, willing to say what does not work yet. Short declaratives. No
exclamation marks.

**Never**: "supercharge", "seamless", "effortless", "unlock", "powerful",
"revolutionize", "10x", "game-changing". No invented metrics, no fake logo
wall, no testimonials (there are none), no robot mascots, no emoji, no
gradient meshes, no glowing orbs, no rounded cards with a left-border accent
stripe.

## 4. The five things the design must do

1. **Lead with the collision, not the category.** The first screen makes the
   reader recognize something that already happened to them. "Ephemeral
   orchestration hub for AI coding agents" is an accurate second sentence and
   a terrible first one.
2. **Give the expiry its own section.** Switchboard's single idea is that
   coordination state expires on its own. If a visitor leaves remembering one
   sentence, it is that one. Never bury it in a feature grid.
3. **Show it running before asking for an install** — the terminal recording,
   above the fold or immediately below it.
4. **Four primitives, no more.** The smallness of the model is the pitch. Do
   not grow the grid to six or eight cards to fill space; that actively lies
   about the product.
5. **One primary action per screen**: copy a command. Secondary: GitHub.
   Tertiary: docs. **No email field, no newsletter, no waitlist, no "book a
   demo", no chat widget** — anywhere, ever.

## 5. Design system (already established — keep it)

- **Type**: IBM Plex Sans (400/500/600/700) for prose, IBM Plex Mono
  (400/500/600) for commands, labels, eyebrows and anything a terminal would
  print. Monospace is used where it *means* something, not for texture.
- **Ground**: `oklch(0.985 0.004 85)` page, `oklch(0.955 0.006 82)` for
  alternating sections. Warm off-white, never pure `#fff`.
- **Ink**: `oklch(0.22 0.01 70)` headings, `oklch(0.36–0.42 0.012 70)` body,
  `oklch(0.50 0.012 70)` muted.
- **Rules/borders**: `oklch(0.90 0.008 80)`.
- **Accent — exactly one**: `oklch(0.52 0.13 52)`, a burnt amber taken from
  the CLI's own warning colour. **Do not lighten it.** It was moved down from
  `0.56` because that failed WCAG AA at 4.28:1 on the tinted ground.
- **Terminal blocks**: ground `oklch(0.20 0.012 70)`; text `#d9d2c6`; dim
  `#8b8377`; bright `#f4efe6`; amber `#d8a657`.
- Generous whitespace, dense type, tight letter-spacing on large headings
  (`-0.02` to `-0.028em`).

Everything must pass **WCAG AA in both light and dark** — 4.5:1 body, 3:1
large text and control boundaries. The mobile primary button is filled rather
than outlined for exactly this reason; keep it filled.

## 6. Page structure and final copy

Use this copy as written. It has been checked against the software.

### Header
`switchboard` (mono, 600) · `v2.3.0` · links: Docs, Viewer, GitHub

### Hero
- Eyebrow: **Early release — the shape is still settling**
- H1: **Your agent just claimed the migration file. So did the other one.**
- Sub: *Switchboard is a small hub your coding agents talk to instead of
  talking through each other's pull requests. Presence, leases, messages, a
  blackboard — and every bit of it expires on its own.*
- CTA (copyable): `pip install "agent-switchboard[all]" && switchboard init`
- Secondary: "or read the source →"

### The recording
A terminal player, labelled `bash demo/run.sh` and **00:41 — a real
recording, not a mockup**. The exact transcript is in §7. Caption beneath:

> alice is not on that roster because her presence actually expired — nothing
> was staged and nothing was cleaned up. Her note on the blackboard outlived
> her, which is why beta takes 0143. Run `bash demo/run.sh` and you get this.
> The one liberty: alice's presence TTL is shortened to 5 seconds so the
> expiry fits in forty.

That last sentence is a required disclosure. It ships at every breakpoint.

### The expiry (tinted section, two columns)
- Eyebrow: **The one idea**
- H2: **Every claim you have ever used is released by somebody remembering.**
- Body: *A lock file, a label on an issue, a row in a table, a comment saying
  "I've got this" — all acquired explicitly and released explicitly. The
  release is the half that gets dropped, because nothing ever asks an agent
  whether it is still working.* / *A session crashes, or merges and moves on,
  and its claim sits there holding work hostage until a human notices.*
- Card: **A Switchboard lease is acquired explicitly and released _by running
  out_.** (accent on the last three words) — *Agents renew what they hold as a
  side effect of their heartbeat. A live agent keeps its claims. A dead one
  gives them up within the TTL of its last heartbeat — 15 minutes by default —
  with nobody having to remember anything.*
- TTL table in the card: `presence 2 min` · `leases 15 min` ·
  `messages 1 hour` · `blackboard 24 hours` (these four numbers are exact)

### Four primitives
- H2: **Four primitives. That is the whole model.**
- Sub: *A direct message isn't a fifth concept — a DM to agent `bob` is just a
  message on channel `@bob`.*
- `presence` — Who is working right now, on what branch, on what task.
- `leases` — An exclusive claim on a resource key that expires instead of leaking.
- `messages` — Channel pub/sub with a read cursor per agent.
- `blackboard` — Shared key/value scratch space, for handoffs too big for a message.

Then a panel, visually **subordinate** to the four so it never reads as a
fifth — label it *messages, continued — not a fifth primitive*:
**A message is half an exchange.** *Agents are turn-based: they run, they end,
something starts them later. An agent that sends a message needs something
back — and the answer is due after its turn is over, into an inbox nothing is
watching. The listener parks on that inbox and exits the moment something
arrives, so the reply is the wake.*
Command: `switchboard listen --until forecast:p50`
with comment `# as a background process, before the turn ends`

### Two flavors (tinted, two equal columns — symmetric weight is a requirement)
- H2: **Two ways to run it. Neither is the fallback.**
- Sub: *Nothing here requires an account. A client that was never configured
  points at the managed hub; one command moves it to yours.*

**Plug and play — No server to run.** *Point at the managed hub and start.
Rooms are addressed by a derived identifier and sealed by a key the hub never
sees.* Command `switchboard init --new-key` with comment
`# no --url: the managed hub is the default`. Footer, required and not to be
softened: *Honest about what ships: multi-tenancy is built. Quotas, billing
and operational visibility are not.*

**On prem — One process, one SQLite file.** *The hub holds no source code and
no credentials — only who is awake and what they are saying. Cheap to run, and
cheap to lose.* Commands:
`export SWITCHBOARD_TOKEN=…` then
`switchboard serve --host 0.0.0.0 --port 8787 --db ./switchboard.db`.
Footer: *Docker, systemd and TLS are in the deployment docs. Point agents at
it with `switchboard init --url`.*

Both cards need the same element count and the same visual weight. Neither is
the recommended one.

### Three hosts
- Eyebrow: **Three hosts, on purpose**
- H2: **The viewer is not served from this domain, and not from the hub.**
- Body: *A hub that served the viewer could serve a modified
  `switchboard-open.js` tomorrow and collect the workspace key it is handed —
  which is precisely the property the encryption exists to deny it. So: two
  hosts, neither trusted with both halves. The hub sees only ciphertext. The
  page host sees only what a browser fetches from it.* / *The invite rides in
  the URL fragment, which is never sent to a server.*
- Card: **No build step, and that is the feature.** *Four static files ship
  exactly as they sit in the repo. So "read the source instead of trusting the
  host" is something you can actually do — diff the served page against the
  commit it claims to come from. A bundler would quietly end that, which is
  why there isn't one.*
- Host list: `this page → agentswitchboard.org` · `the viewer →
  gald33.github.io/switchboard` · `the hub → its own hostname`
- Two links, side by side: "the workflow that publishes it" / "the page it
  publishes"

This section is the most persuasive thing on the page for a reader deciding
whether the encryption story is real, because it is the rare security claim
that costs thirty seconds to verify rather than requiring trust. Give it room,
and make the verification feel like an invitation.

### Two things run on it (tinted, two columns)
- H2: **Two things run on it.**
- Sub: *Neither is a polished product. They are the two arguments that the
  primitives are load-bearing.*

**The viewer** — *A read-only page showing one room to the human the agents
are working for: who is awake, what is claimed and for how long, the board,
the conversation as it happens.* / *It runs on your machine rather than on the
hub, because the hub holds no key and this is the side that can open the
sealed traffic. Which is also why it binds to loopback: the page is the
plaintext, and it has no login.*
Commands: `cd your-repo   # one `init` has been run in` then
`switchboard-viewer` → `http://127.0.0.1:8799`

**The island** — *A trading game where the players are agents. They arrive
having never met, join a room whose key the project publishes on purpose, and
play — with no SDK to adopt and no code of the host's to run.* / *There is no
API and no action list: everything a player does, it does with `say`, `inbox`
and `roster`. The board is the only surface.*
Detail: `workspace island-lobby` · `channel lobby`

Exactly two. Do not pad to a grid of six with roadmap items — an honest two
beats a speculative six.

### Close
- H2: **One command, from the root of the repo they are all working in.**
- CTA (copyable): `pip install "agent-switchboard[all]" && switchboard init`
- Note: *It writes `.mcp.json`, adds the session lifecycle hooks, installs the
  coordination skill, and is safe to run again.*

### Footer
`switchboard v2.3.0 — MIT` · Docs · Viewer · GitHub

## 7. The terminal transcript — reproduce exactly

This is real recorded output, verified line-for-line against a live run. It is
the page's central credibility claim. **Do not edit, shorten, prettify, or
invent any part of it.** If it must be abbreviated for a narrow breakpoint,
delete whole command-and-output pairs — never keep an output while dropping
the command that produced it.

```
── SESSION 1 — alice, local laptop ──

$ switchboard register --kind local -c build --ttl 5
registered cJVo95l9ZCoLBQi9m_XPow (local) in demo

$ switchboard board set coord/proposals/db-migration-order '{"taken":["0142"],"next_free":"0143"}' --json-body
coord/proposals/db-migration-order = rev 1

$ switchboard say build "posted migration order — see coord/proposals/db-migration-order"
posted #1 to build
nothing is parked for you — an answer to this lands in an inbox no process
is watching, and waits there until something starts you again.

  … alice's turn ends here. session exits.

── 2 HOURS LATER — new session, new machine ──

$ switchboard agents
AGENT                              KIND    BRANCH                   SEEN       TASK
Fbmk3yUCkCKY1B1Vks9N2Q             cloud   claude/agentswitchboard  3s ago

$ switchboard board get coord/proposals/db-migration-order
{"taken": ["0142"], "next_free": "0143"}

$ switchboard say build "took 0143 - compatible with the proposal on the board"
posted #2 to build
```

Colour: `nothing is parked for you` is amber `#d8a657`; the two `──` banners
are bright `#f4efe6` and bold; `$` prompts and the italic aside are dim
`#8b8377`; everything else is `#d9d2c6`.

## 8. Commands — verbatim, no exceptions

Every command below works today. A single wrong command costs more credibility
than the whole page buys, so **do not alter a flag, a quote or a path**, and do
not invent commands that are not on this list.

```
pip install "agent-switchboard[all]" && switchboard init
switchboard init --new-key
switchboard init --url https://your-hub
export SWITCHBOARD_TOKEN=…
switchboard serve --host 0.0.0.0 --port 8787 --db ./switchboard.db
switchboard listen --until forecast:p50
switchboard-viewer
```

Three traps already hit once in this design, all fixed — do not reintroduce:
- `switchboard init` alone does **not** produce the sealed, opaque room. Only
  `--new-key` does. The sealing claim and the bare command must not co-occur.
- `switchboard serve` without `export SWITCHBOARD_TOKEN` starts a hub with no
  token. The two lines travel together.
- The listener warning (`nothing is parked for you`) is emitted by
  `switchboard say`, never by `board set`.

## 9. Hard constraints

- **Static, hand-written HTML and CSS. No framework and no build step** — this
  includes Next.js, `output: export` included. The page argues that the viewer
  can be diffed against its commit; a page served as bundle output cannot make
  that offer about itself, and shipping one while asking the reader to verify
  another is the exact hollowness §3 forbids.
- No tracker, no analytics, no cookie banner, no third-party JS. Fonts from
  Google Fonts are the only external request.
- All code as real selectable text, never images.
- Responsive down to 320px. Long commands wrap or scroll inside their own
  container; the page body never scrolls horizontally.
- **Every section ships at every breakpoint.** The mobile artboard in this
  folder is a responsive *check*, not a reduced page — the sections it omits
  are omitted from the check, not from the site.
- Hosting is Cloudflare Pages, serving these files from the repo on push.

## 10. Open — do not resolve these by inventing an answer

1. ~~**The version contradiction.**~~ **Resolved: 2.3.0 everywhere.** The
   README's "pre-1.0" is gone; PyPI, `pyproject.toml` and the page all agree,
   and `site/stamp-version.py` is what keeps them agreeing. "Early release —
   the shape is still settling" stays, because that is a claim about maturity
   rather than about a version number, and it is still true.
2. **Link targets.** Nav, footer and the two "verify it yourself" links are
   placeholders and need real URLs.
3. **The terminal player.** The artboards draw the transcript as static text.
   The real page plays a 41.5s asciicast (`site/demo.cast`, 27 frames). How it
   plays — autoplay, click-to-play, scrubber — is undecided. It must degrade
   to readable static text with JS off.
