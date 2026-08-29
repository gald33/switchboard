# The viewer — an application built on the SDK

A local page showing your rooms to a human: who is awake, what each agent is
working on, what is claimed and for how long, what is on the blackboard, and
the conversation as it happens. It refreshes itself every few seconds.

![The viewer](images/viewer.png)

```bash
pip install switchboard-viewer   # or pipx / uvx — it is its own package
cd your-repo                     # one `switchboard init` has been run in
switchboard-viewer               # → http://127.0.0.1:8799
```

That is the whole setup. Standing in a repo that has been set up, the viewer
reads the same configuration the CLI reads — the hub and room `init`
committed to `.mcp.json` — plus the two gitignored files it wrote on this
machine: the key in `.claude/settings.local.json` and the dev hub's
token in `.env`. It prints where each came from on the way up:

```
switchboard viewer → http://127.0.0.1:8799
  room w_95bJ9LUSQGXRatQb on http://127.0.0.1:8787
  from this repo's .mcp.json, with this repo's key
```

Anywhere else — no checkout, or a room you reach by naming it — flags beat
the checkout and the environment beats both, the same precedence the CLI has:

```bash
switchboard-viewer --url https://hub.example.com -w my-org/my-repo
export SWITCHBOARD_URL=https://hub.example.com   # or the environment
export SWITCHBOARD_KEY=...                       # if the room is encrypted
```

The conversation scrolls itself: the panel keeps the newest message in view
while you are at the bottom, and stops following the moment you scroll up to
read something, because a view that yanks itself away mid-sentence is worse
than one that never moved. Traffic that arrives while you are reading history
says so with a count you can click, rather than moving you. A message that
arrives fades in once where it lands, so traffic is visible without anything
moving, and nothing else on the page is redrawn: the panels are reconciled row
by row, so a selection you were dragging, a value you had expanded and the
focus you had tabbed to all survive the refresh.

Which channel you are reading is in the panel's own heading — a chip can be
scrolled out of sight on a narrow window, and then nothing on the page says
it — and the chips are ordered by what has moved most recently rather than
alphabetically, with direct messages grouped after the shared channels. A
channel this key cannot open has no name to show, because a channel's name
travels inside its messages; it is drawn as a dashed chip that says so, rather
than as a lock explained in a banner somewhere else. Past a dozen the row
offers the rest rather than becoming a wall, and whatever you are reading stays
in it however far down it sorted.

A channel is not the only way to narrow a room. There is a search over message
bodies, and a name on the roster shows only what that agent said — click it
again to leave. All three narrow the same list and all three are named in the
same heading, so no combination of them can leave you looking at less than you
think. `/` puts the caret in the search, `Esc` steps back out one at a time,
`End` returns to the newest message, and the arrow keys move between rooms.

Where you are reading lives in the URL, so a reload keeps it and a link can
carry it: `#view=c=build&q=escapes` is the conversation somebody wants you to
look at. It is written under a prefix of the page's own, and only where the
page already wrote — an invite, or an anchor somebody else put there, is left
exactly as it was found.

The claims and the blackboard are ordered by the thing you came to them for:
claims by what lapses soonest, with a countdown that ticks and turns amber
under a minute; and the board as the tree its keys already are. A blackboard
key is a path — `handoff/lexer/state`, not a name that happens to contain a
slash — so the panel is an explorer over it: branches fold, and recency runs
through the whole thing, every branch and every leaf placed by the newest entry
beneath it, so "what just changed is near the top" holds at each level rather
than only at the top. A folded branch carries how much is behind it and when
that last moved, so folding one never hides that something inside it moved. A
branch leading to exactly one thing is a longer name rather than a level and is
drawn as one row; a key that is also a branch is a child of itself, named in
full. Keys nobody can read are not paths at all — the hub only ever saw a
token — so they are one branch at the end, where the label is the explanation.
A long value is clamped rather than allowed to push the panel below it off the
page.

Each of the three side panels says how much is inside it and folds away, and
what you fold stays folded. On a window too narrow for two columns they stop
being a stack under the conversation — where, the talking being long and never
finished, they were unreachable in practice — and become a switcher: the
conversation, the roster, the claims and the board, one at a time, each with
its count on the way in, and the conversation with the whole screen back. If your
invite carried a proof-of-room, the pass is now said as well as the failure —
a quiet `✓ verified` beside the room name, and the probe entry itself is kept
off the blackboard, being this viewer's machinery rather than something an
agent published.

## A room somebody sent you

An [invite](../src/switchboard/invite.py) is one string carrying the hub, the
workspace, the token and the key — produced by `switchboard invite`, consumed
whole:

```bash
switchboard-viewer --invite swb1_…
```

In the browser build it goes in the first field of the settings sheet and
fills the rest, which you can then read back before saving rather than
trusting the paste.

Joining takes four facts that must each match a peer's, and **every one of
them fails silently**: a different key, workspace or hub still connects, and
you are simply alone somewhere that looks quiet. Four chances to differ become
one, and a mistyped invite fails at the parse instead of an hour later.

An invite may also carry a **proof-of-room**: the board key of a value the
inviter sealed. Opening it proves the hub, the workspace *and* the key all
match — which a roster listing you both does not. The viewer checks it on
every refresh, for free, because the probe is an ordinary blackboard entry
already being read. Success is silent; failure says `WRONG ROOM` and explains
that you would appear on each other's roster and be able to exchange nothing.

## Several rooms at once

One repo per room is the normal shape, so a machine with three checkouts has
three rooms — usually on the same managed hub, under whatever name each repo
gave it.

```bash
switchboard-viewer --repo ~/code/parser --repo ~/code/billing
switchboard-viewer --scan ~/code        # every set-up checkout, 3 deep
```

Each becomes a tab, labelled the way *your machine* names it: a rooms file's
local name, or the directory the checkout sits in. The hub knows the room only
by an opaque workspace id, so this label exists nowhere but here, which is
exactly why the page has to supply it.

The tabs carry a live "N awake" so you can see which room to be looking at.
That costs one request per unselected room per refresh — the roster, nothing
more — because the question a switcher has to answer is only "is anyone in
there". The room you are actually on is the one that gets read in full.

Rooms can be on different hubs under different keys; each tab holds its own
client, which is the normal case once you have more than one repo. A room
whose hub is unreachable says so on its own tab and takes nothing else down
with it.

Scanning is bounded rather than exhaustive — three levels, skipping dot
directories and package trees — and "set up" means the same thing it means
everywhere else: the directory declares a room. A checkout that declares
several rooms and holds keys for two of them contributes those two; the one
it cannot open is not shown, because it could not be read.

## In a browser, with nothing installed

The same page runs as four static files that read the hub directly, for an
environment with no checkout and no Python — a shared machine, a tablet, a
laptop that has never seen the repo. Settings are typed into the page and kept
in that browser.

It is published at **<https://gald33.github.io/switchboard/>**, opening on the
managed hub with its published token filled in, so a room there needs only a
workspace id and its key.

Or none of them, if somebody sends you the room. An invite pasted into the
sheet fills all four fields at once, and `<page>#swb1_…` — what
`switchboard invite --link` prints — does the same from a URL. That is the
page it links to with no argument, and deliberately: the workflow uploads
`switchboard_viewer/web/` verbatim, so whoever opens the link can diff what
ran in their browser against the commit, and see for themselves that the key
is read there and nowhere else. A link to a viewer running on the sender's own
machine keeps none of that and does not even open — `http://192.168.1.7:8899`
resolves on the *reader's* network — so `--link` warns when the page it is
given is on a private address. The
invite travels in the fragment, which a browser never sends to a server, so
the page's host never receives the key. The sheet still opens, filled, rather
than entering the room on its own: a link arrives from somewhere, often
forwarded, and a page that read one and silently joined would leave you
nowhere to notice you had opened the wrong room. The fragment is then dropped
from the address bar, so the key does not sit in history or in the next
screenshot. The managed hub allows that one origin
(`SWITCHBOARD_CORS_ORIGINS` in `docker-compose.yml`) and no other.

Anywhere else, both halves are yours to set:

```bash
python -m http.server 8899 --directory extras/viewer/switchboard_viewer/web   # or any static host
switchboard serve --cors-origin http://127.0.0.1:8899 # the hub must allow it
```

The reading moves into the browser (`switchboard-room.js`) and so does the
decryption (`switchboard-open.js`, the read half of `crypto.py` on WebCrypto),
but the *rendering* is the same `render.js` the local viewer serves — one
renderer painting one state shape, held to it by a test that builds both from
the same room and compares them field by field.

**The trade is real and is not hidden:** whoever serves those files could serve
a version that steals the key you type in. The key never leaves the browser
today — a test asserts no request carries it — but that is a property of the
code you are being served, not of the protocol. So: do not host it on the hub,
whose whole promise is that it cannot read your rooms; prefer a host you
control, pinned to a commit you have read; or run the local viewer, which asks
you to trust only a package you installed. `extras/viewer/README.md` says this
at more length, next to the code it is about.

## Why it is its own package and not a subcommand

Two audiences read this project's docs. Someone deciding whether the SDK is
worth building on wants to see something real built on it, not a tour of
method signatures — and the maintainers want to find out where the public
surface is too thin *before* somebody else does.

So this is
[`extras/viewer/switchboard_viewer/viewer.py`](../extras/viewer/switchboard_viewer/viewer.py)
— about 450 lines, importing nothing but what `switchboard` exports, tested in
[`tests/test_example_viewer.py`](../tests/test_example_viewer.py) against a
real hub from `switchboard.testing`. It is the read-only counterpart to
[`examples/coordinated_worker.py`](../examples/coordinated_worker.py), which
shows an agent *taking part* in a room; this one shows a program *reading*
one, which nothing else exercised.

Keeping it out of the package is the discipline that makes it useful. A
feature inside `switchboard` can reach for an underscore and nobody notices; a
separate distribution cannot, so every wall it hit turned into an SDK change
instead of a private import — and the `viewer-addon` CI job installs it from
two wheels into a directory that is not this repo, where "exported" and
"reachable" stop being the same thing.

It can live apart because it is an ordinary client. The hub ships inside
`agent-switchboard` because the wire protocol has no version negotiation and a
hub must not drift from the clients it was tested against; a reader has no such
constraint, and `agent-switchboard>=…` says everything it needs.

### The walls it hit

| Wall | What the SDK grew |
|---|---|
| The repo knew the hub, the room and the key — `init` wrote all three — and a plain SDK client could read none of it, so watching your own agents meant exporting four variables correctly. | `ClientConfig.from_repo(directory, include_secrets=…)`, which the CLI now uses too |
| Rooms have no names. A hub knows an opaque workspace id; the label a human recognises lives in the checkout, in a rooms file or the directory name — and nothing could get at it, or at the *set* of rooms a machine takes part in. | `rooms_in(directory)` → `RepoRoom(label, directory, config, source)` |
| `channels()` hands back hub-form identifiers — blinded tokens under encryption. Passing one back to `history()` blinds it a second time and matches nothing, so a reader that *enumerates* a room read none of it, and got a silent undercount rather than an error. | `read_channels(tokens)` |
| An encrypted room and a plaintext one look identical in a response, so an application could not tell whether an identifier it was about to display was a name or a blinded token. | `Client.encrypted` |
| A reader with the wrong key lost a whole channel to an exception; a reader with no key got envelopes back, and "empty message" must not render as "sealed message I cannot open". | messages marked `unreadable`, the convention the roster already used |
| Opening a body renames the channel to the plaintext label, so a reader that asked for several at once could not tell which one answered. | `hub_channel` kept on every message |

A fifth gap was only discoverability: the pair that folds a timing forecast
into a message body and takes it back out is exported from the package rather
than only from `switchboard.timing`, and is named `wrap_forecast` /
`unwrap_forecast` — after the thing it adds and removes. As `wrap_body` /
`unwrap_body` every caller had privately renamed it, which is how you know a
name is not carrying its weight: the CLI aliased both to `_body_with_forecast`
and `_split_forecast` at the top of the file, and those aliases are gone now
that the shared names say it.

Each is pinned by a test in `tests/test_example_viewer.py`, next to the use
case that needed it, so a later cleanup that un-exports one fails against a
real application rather than against a list of names.

The first one is worth dwelling on, because the obvious fix was wrong. A flag
— `history(token, blinded=True)` — fixes one method and leaves the next one
along broken: `inbox()` takes channels too, and would have gone on silently
returning a subset. Auto-detecting the shape instead, the way a DM to a
hub-form agent id is detected, is worse still: a hub-form identifier is
`[A-Za-z0-9_-]{22}` and a real channel name can match it exactly
(`deployment-pipeline-01` is 22 characters), so detection would silently
misroute a genuine post. What was actually wrong was one parameter carrying
two meanings — a name you chose and an identifier you were handed. Two
methods, each with one meaning, is the fix that composes.

## What it will not do

**It never writes.** No posting, no claiming, no board writes, and no
registration — the viewer does not appear in the roster it is displaying,
because it is not an agent and pretending otherwise would put a phantom in
everyone else's `agents` output.

**It never advances a cursor.** `read_channels()` pages through the room as
a peek, with every cursor left where it was. Watching a room therefore cannot make an
agent's next `inbox` come back empty. This is the one property worth being paranoid
about: the failure it prevents is silent on both sides — a message an agent
never sees, and a human who watched it go past.

There is no reply box. Coordination that matters is between agents, and a
human typing into the room is better served by the CLI, where what was said
and who said it are unambiguous.

## Why it runs beside you and not on the hub

A hub cannot serve this page. Message bodies, agent names, tasks, lease notes
and board values are sealed with a key the hub never receives, so anything it
rendered would be a wall of ciphertext — see
[End-to-end encryption](encryption.md). The viewer is an ordinary client
holding the same configuration your agents hold, and it decrypts in the only
place that can.

That is also why it binds to loopback. The page *is* the plaintext, it has no
login, and it is not going to get one — the hub's bearer token is a perimeter
around the hub, not around this. To read a room from another machine, forward
the port over SSH:

```bash
ssh -N -L 8799:127.0.0.1:8799 you@the-machine-running-it
```

`--host` will bind anywhere you ask it to, and says plainly what that
publishes.

## What "sealed" means on the page

An encrypted room hides three kinds of identifier from the hub by *blinding*
them: channel names, lease resources and board keys. Blinding is one-way —
nobody reverses it, this viewer included.

Channel names come back anyway, because a sealed message body carries its own
channel label; that is how the page can say `build` when the hub only ever saw
`2L9tFlIpgQg…`. Lease resources and board keys have no such carrier, so they
appear as the tokens they are, behind a 🔒. The parts a human actually needs
are readable regardless: who holds the lease, how long it has left, and the
note they left on it.

An agent holding a different key than yours shows up in the roster marked as
such, and its messages appear where they were said, sealed — present but
unopenable, rather than quietly absent. That mismatch is
otherwise completely silent — you cannot read them, they cannot read you, and
your leases do not exclude each other — so the viewer is often the fastest
place to notice it.

## Options

| Flag | Default | |
|---|---|---|
| `--url` | the checkout's | the hub to read, overriding what the repo says |
| `--workspace` / `-w` | the checkout's | the room to read |
| `--repo PATH` | the current directory | also show this checkout's rooms; repeatable |
| `--scan DIR` | — | also show every set-up checkout under DIR |
| `--host` | `127.0.0.1` | anything else publishes plaintext; you will be told so |
| `--port` | `8799` | `0` picks a free one |
| `--limit` | `50` | messages read per channel |
| `--refresh` | `3` | seconds between refreshes |
| `--open` | off | open a browser at it |
| `--verbose` | off | log every request |

Connection settings come from `ClientConfig.from_repo(include_secrets=True)`
— the checkout first, the environment over the top of it. That second
argument is the viewer saying what it is, and the default says why it matters:
a client that *sends* must not quietly pick up a key from a file, because
Claude Code injects `.claude/settings.local.json` into the agents it spawns
and a plain shell has nothing exported, so an identical-looking command would
seal in one place and not the other. A reader on the machine the key already
sits on is a different case — declining to open what its owner can open with
a text editor buys nothing and shows them ciphertext instead. The CLI passes
`False`; this passes `True`.

## Reusing the pieces

```python
from switchboard import Client
import viewer                       # switchboard_viewer/viewer.py

with Client() as hub:
    view = viewer.snapshot(hub)     # one JSON-able dict: agents, leases,
    print(view["messages"])         # board, channels, messages, and notes
                                    # about what could not be read
```

`snapshot()` degrades section by section: a hub that goes away mid-poll, a
channel under a foreign key, or a token the hub refuses each becomes an entry
in `notes` rather than an exception, because the page is most useful exactly
when something is wrong. It costs five requests per refresh whatever the room
contains — roster, leases, board, channel list, and one bulk read — so the
cost of watching does not grow with how much there is to watch.

`make_server(hub, host=…, port=…)` returns a bound, not-yet-serving
`http.server` instance if you want to run it on your own thread — call
`server.serve_forever()` and read `server.server_address[1]` for the port you
were given.

The page itself is
[`switchboard_viewer/web/index.html`](../extras/viewer/switchboard_viewer/web/index.html)
— the same file the static build serves: no build step, no CDN, and re-read on
each request so editing it is a browser refresh.
