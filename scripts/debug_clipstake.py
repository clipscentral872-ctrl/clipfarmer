"""One-shot diagnostic — log into ClipStake, dump the marketplace page.

Writes:
  data/debug/clipstake_marketplace.html  — full DOM
  data/debug/clipstake_marketplace.png   — full-page screenshot
  data/debug/clipstake_cards.json        — what our current harvester sees

Useful when the scraper finds 0 cards and we need to update the selectors.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.clipstake_session import ClipStakeSession, _CLIPSTAKE_HARVEST_JS


def main() -> int:
    debug_dir = Path("data/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)

    with ClipStakeSession() as sess:
        # Context-manager entry already restores the cached session.
        page = sess.page
        # `commit` returns as soon as navigation commits — doesn't wait for any
        # network or DOM event.  Then we wait_for_timeout for the React render.
        # ClipStake's marketplace bundle is heavy; 30s domcontentloaded was flaky.
        try:
            page.goto(sess.marketplace_url, wait_until="commit", timeout=60_000)
        except Exception as e:
            logger.warning(f"[clipstake-debug] initial nav failed ({e}); retrying")
            page.goto(sess.marketplace_url, wait_until="commit", timeout=60_000)
        page.wait_for_timeout(8000)

        # Lazy-load scroll — keep going while page height grows OR card count grows.
        prev_card_count = -1
        prev_height = -1
        stable_iters = 0
        for i in range(40):
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(1000)
            try:
                h = page.evaluate("document.body.scrollHeight")
                n = page.evaluate("document.querySelectorAll('a.MuiCard-root[href^=\"/marketplace/\"]').length")
            except Exception:
                h, n = 0, 0
            if h == prev_height and n == prev_card_count:
                stable_iters += 1
                if stable_iters >= 3:
                    logger.info(f"[clipstake-debug] scroll done at iter {i} — {n} cards, height {h}")
                    break
            else:
                stable_iters = 0
            prev_height = h
            prev_card_count = n

        html_path = debug_dir / "clipstake_marketplace.html"
        png_path = debug_dir / "clipstake_marketplace.png"
        json_path = debug_dir / "clipstake_cards.json"

        html_path.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(png_path), full_page=True)

        cards = page.evaluate(_CLIPSTAKE_HARVEST_JS)
        json_path.write_text(json.dumps(cards, indent=2), encoding="utf-8")

        logger.info(f"[clipstake-debug] wrote HTML  -> {html_path.resolve()}  ({html_path.stat().st_size:,} bytes)")
        logger.info(f"[clipstake-debug] wrote PNG   -> {png_path.resolve()}")
        logger.info(f"[clipstake-debug] harvester found {len(cards)} card(s); JSON -> {json_path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
