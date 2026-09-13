#!/usr/bin/env python3
"""Render site/design/og-card.html into site/img/og.jpg.

    python3 site/design/render-og.py

The card carries the page's headline, its subtitle and the four expiry times.
When the hero copy changes, the card is stale until this is re-run — a link
preview showing a sentence the page no longer contains is the same quiet lie
`site/check-quotes.py` exists to prevent, one layer out.

Rendered at 2× and downscaled, which is what `site/img/README.md` has always
said the card is: text at 1200px wide is thin and slightly ragged straight out
of a headless browser, and a Lanczos downscale from 2400px is not.

Needs Playwright with Chromium (`pip install playwright && playwright install
chromium`) and Pillow, and it fetches the two IBM Plex families from Google
while it renders — the same fonts the page loads. Without the network the card
still renders, in the fallback stack, which is not what ships: the script
checks and refuses rather than quietly writing a card in the wrong face.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

SITE = Path(__file__).resolve().parent.parent
CARD = SITE / "design" / "og-card.html"
OUT = SITE / "img" / "og.jpg"
WIDTH, HEIGHT, SCALE = 1200, 630, 2


def main() -> int:
    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=SCALE,
        )
        page.goto(CARD.as_uri())
        page.wait_for_load_state("networkidle")

        # Did the real face arrive? `document.fonts.check` answers for the
        # family the card actually asks for, which is the thing a fallback
        # silently replaces.
        got_plex = page.evaluate(
            "document.fonts.check('600 66px \"IBM Plex Sans\"')"
            " && document.fonts.check('400 21px \"IBM Plex Mono\"')"
        )
        if not got_plex:
            print("IBM Plex did not load — the card would ship in a fallback face."
                  "\nCheck the network and re-run; nothing was written.", file=sys.stderr)
            browser.close()
            return 1

        shot = page.screenshot(type="png")
        browser.close()

    big = Image.open(io.BytesIO(shot)).convert("RGB")
    if big.size != (WIDTH * SCALE, HEIGHT * SCALE):
        print(f"expected {WIDTH * SCALE}×{HEIGHT * SCALE}, got {big.size[0]}×{big.size[1]}",
              file=sys.stderr)
        return 1

    big.resize((WIDTH, HEIGHT), Image.LANCZOS).save(OUT, "JPEG", quality=92, optimize=True)
    print(f"wrote {OUT.relative_to(SITE.parent)} — {WIDTH}×{HEIGHT}, "
          f"{OUT.stat().st_size // 1024} KB")
    print("og:image:width and og:image:height in index.html must match those numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
