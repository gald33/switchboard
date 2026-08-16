# The viewer, in a browser

The same page as [`examples/viewer.py`](../viewer.py), doing the reading in
the browser instead of in a local Python process. Four static files, no build
step, no server of its own:

| | |
|---|---|
| `index.html` | the page — markup, styling, and which of the two data paths to use |
| `render.js` | painting a room. Shared with the local viewer, which is what keeps them one product |
| `switchboard-room.js` | reading a hub and assembling the view, in the browser |
| `switchboard-open.js` | the read half of the cipher, on WebCrypto |

## The published one

<https://gald33.github.io/switchboard/> — this directory, at the commit on
`main`, deployed by [`.github/workflows/pages.yml`](../../.github/workflows/pages.yml)
with no build step, so what is served can be diffed against the commit it
claims to come from.

It opens on the managed hub with its published token already filled in, so
reading a room there takes a workspace id and — if the room is encrypted — the
key. Both come from the checkout that coordinates in it:

```bash
switchboard whoami          # workspace, and whether it is encrypted
echo "$SWITCHBOARD_KEY"     # or the `key` in .mcp.json / .env
```

## Running it yourself

Anything that serves static files:

```bash
python -m http.server 8899 --directory examples/web
```

Then open it and enter a hub, a workspace and — if the room is encrypted —
the key. Settings live in that browser's `localStorage` and nowhere
else; add several rooms and they become tabs.

## The hub has to allow your origin

A browser refuses a cross-origin read before it is sent, whatever credentials
the page holds. So the hub needs to be told which page may read it:

```bash
switchboard serve --cors-origin https://you.github.io
# or
SWITCHBOARD_CORS_ORIGINS=https://you.github.io,http://127.0.0.1:8899 switchboard serve
```

Off by default in the library, and set in `docker-compose.yml` to
`https://gald33.github.io` — the origin above — so the managed hub allows the
published page and nothing else. Override it in `.env` if you host the page
elsewhere, or set it empty to refuse browsers entirely. A hub with no browser
client should not carry the attack surface of one. `*` is accepted and is defensible here in a way it usually is
not — this API authenticates with an `Authorization` header rather than a
cookie, so a hostile page gains nothing from being allowed to make a request
it cannot authenticate.

## What you are trusting, stated plainly

**Your key is typed into a page that somebody serves you.** It never leaves
the browser — `tests/test_web_page.py` asserts that no request carries it, and
the hub could not use it if it arrived — but whoever serves these files could
serve a different `switchboard-open.js` tomorrow that posts the key somewhere.
That is true of every browser-side crypto tool and it cannot be fixed from
inside one.

What follows from that:

- **Do not host it on the hub.** This is why the page is on GitHub Pages
  rather than on `switchboard.lucille-ai.com`, which would have been one line
  of config and no workflow at all. A hub that serves this page can read the room
  it is hosting, by serving one modified script, which is precisely the
  property the project exists to provide. The `--cors-origin` route keeps the
  two parties separate: the hub can be untrusted with your content, the page
  host can be untrusted with your traffic, and neither is trusted with both.
- **Prefer a host you control**, pinned to a commit you have read. These files
  are small and dependency-free on purpose: they are meant to be read.
- **Or run the local viewer** — `python examples/viewer.py` — which asks you
  to trust only a package you installed, and reads your checkout so there is
  nothing to type at all.

Over plain HTTP the page says so and refuses to pretend: WebCrypto is not
available on an insecure origin, and a key typed into an unencrypted page is a
key handed to anyone on the path.

## What it does not do

Everything the local viewer does not do, for the same reasons: it never posts,
never registers, and reads through `since=0` with `peek`, so watching a room
cannot advance any agent's cursor or make its next `inbox` come back empty.
