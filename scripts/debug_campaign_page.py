"""One-off: open a Whop content-rewards page, wait long enough for
campaigns to render, save the fully-loaded HTML + screenshot.

Usage:
    python scripts/debug_campaign_page.py <url>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.whop_login import WhopSession


def main(url: str) -> None:
    out_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)

    with WhopSession(headless=True) as session:
        page = session.page
        logger.info(f"goto {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Give the SPA plenty of time to fetch + render the campaign list.
        logger.info("waiting 20s for campaign list to render...")
        time.sleep(20)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Whop has a persistent "Join Our Community" promo overlay that
        # blocks the campaign list. Hide only fixed-position overlays via JS,
        # which is much less invasive than blanket-hiding every class*=modal.
        page.evaluate("""
            (() => {
                const candidates = document.querySelectorAll('div, section');
                candidates.forEach(el => {
                    const cs = window.getComputedStyle(el);
                    if ((cs.position === 'fixed' || cs.position === 'sticky')
                        && el.offsetWidth > 200
                        && el.offsetHeight > 100
                        && el.innerText
                        && /join/i.test(el.innerText)) {
                        el.style.display = 'none';
                    }
                });
            })();
        """)
        time.sleep(1)

        # Scroll a few times in case it lazy-loads.
        for _ in range(5):
            page.mouse.wheel(0, 1500)
            time.sleep(1.5)

        slug = url.rstrip("/").replace("https://", "").replace("/", "_").replace(":", "_")[:80]
        html_path = out_dir / f"deep__{slug}.html"
        png_path = out_dir / f"deep__{slug}.png"
        html_path.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(png_path), full_page=True)
        logger.info(f"saved {html_path.name} and {png_path.name}")

        # Inspect every frame in the page and dump any apps.whop.com frame.
        for f in page.frames:
            if "apps.whop.com" not in (f.url or ""):
                continue
            logger.info(f"frame: {f.url}")
            try:
                frame_html = f.content()
            except Exception as e:
                logger.warning(f"could not read frame content: {e}")
                continue
            frame_slug = (f.url or "frame").replace("https://", "").replace("/", "_").replace(":", "_")[:80]
            (out_dir / f"frame__{frame_slug}.html").write_text(frame_html, encoding="utf-8")
            logger.info(f"saved frame__{frame_slug}.html ({len(frame_html):,} chars)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/debug_campaign_page.py <url>")
        sys.exit(2)
    main(sys.argv[1])
