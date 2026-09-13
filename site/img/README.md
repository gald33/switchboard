# Icons and the social card

**`favicon.svg`** is the source. The PNGs are rendered from it, so edit the SVG
and re-render rather than touching them.

Two agents in contact and one that has gone quiet — the page's own argument, at
the only size a favicon has to survive. The third node sits **off-centre on
purpose**: centred under the other two it reads as a face, which is what the
first draft did.

**`og.jpg`** is 1200×630, rendered at 2× from a card built with the site's own
tokens and fonts, then downscaled. `og:image:width` and `og:image:height` in
the head must match it — several scrapers trust the tags over the file.

**The card has a source now**, which it did not when this file was first
written: [`../design/og-card.html`](../design/og-card.html), rendered by
[`../design/render-og.py`](../design/render-og.py).

```bash
python3 site/design/render-og.py
```

It refuses to write if IBM Plex has not loaded, because a card that silently
ships in a fallback face is worse than one that is not rebuilt.

## Re-rendering

Both come from HTML rendered with Playwright and a local Chrome. Sizes:
`apple-touch-icon.png` 180, `favicon-32.png` 32, `favicon-16.png` 16.

If the card's copy changes, keep it in step with the page — and the page's
copy changing counts. It carries the hero line, the plain-language subtitle and
the four expiry times, and those numbers are the same claim `switchboard init`
has to keep true. The hero line moved on 2026-09-13 and the card was
re-rendered in the same commit; a preview showing a sentence the page no longer
contains is the quiet kind of wrong this repository keeps scripts to avoid.
