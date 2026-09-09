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

## Re-rendering

Both come from HTML rendered with Playwright and a local Chrome. Sizes:
`apple-touch-icon.png` 180, `favicon-32.png` 32, `favicon-16.png` 16.

If the card's copy changes, keep it in step with the page. It currently carries
the hero line, the plain-language subtitle and the four expiry times, and those
numbers are the same claim `switchboard init` has to keep true.
