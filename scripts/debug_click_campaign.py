"""Click into the first campaign card and capture the detail page HTML/screenshot.

Lets us see what URL a campaign lives at and what data (source videos,
rules, submit form) is on its detail page.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.whop_login import WhopSession

LISTING_URL = "https://whop.com/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/"
OUT = Path("data/debug")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with WhopSession(headless=False) as session:
        page = session.page
        logger.info("goto listings")
        page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=30_000)

        # Take an early screenshot so we can see what page is actually rendering.
        time.sleep(8)
        page.screenshot(path=str(OUT / "click__early.png"), full_page=True)
        logger.info(f"early page url: {page.url}; title: {page.title()}")

        # The Whop iframe is named "active-app-container" and starts at
        # about:blank, then navigates to apps.whop.com/.../browse-campaigns.
        # Poll until its URL is set to a real apps.whop.com URL.
        frame = None
        deadline = time.time() + 90
        while time.time() < deadline:
            for f in page.frames:
                u = f.url or ""
                if "apps.whop.com" in u:
                    frame = f
                    logger.info(f"frame ready: {u}")
                    break
            if frame:
                break
            time.sleep(1.0)
        if not frame:
            logger.error("apps.whop.com frame never loaded; here are all frames:")
            for f in page.frames:
                logger.info(f"  frame: name={f.name!r} url={f.url!r}")
            page.screenshot(path=str(OUT / "click__noframe.png"), full_page=True)
            return
        logger.info(f"found frame: {frame.url}")
        # Wait for actual cards inside the frame.
        try:
            frame.wait_for_selector(".campaign-card-bg", timeout=30_000)
        except Exception as e:
            logger.error(f"cards never rendered: {e}")
            (OUT / "click__noCards_frame.html").write_text(frame.content(), encoding="utf-8")
            return
        time.sleep(3)
        # Save the frame HTML for forensic comparison.
        (OUT / "click__cards_frame.html").write_text(frame.content(), encoding="utf-8")
        logger.info("saved frame html post-wait")

        # Click the first .campaign-card-bg
        cards = frame.locator(".campaign-card-bg")
        n = cards.count()
        logger.info(f"{n} cards available")
        if n == 0:
            return
        before_urls = {f2.url for f2 in page.frames}
        cards.nth(0).click(timeout=5_000)
        logger.info("clicked first card; waiting 8s for detail to load")
        time.sleep(8)

        # Detail might be a new frame, a URL change in the existing frame,
        # a route change on the parent page, or a modal in the frame.
        page_url_after = page.url
        logger.info(f"page url after click: {page_url_after}")
        for f2 in page.frames:
            if "apps.whop.com" in (f2.url or ""):
                logger.info(f"app frame after click: {f2.url}")

        # Save parent page and frame contents.
        (OUT / "detail__parent.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / "detail__parent.png"), full_page=True)
        for f2 in page.frames:
            u = f2.url or ""
            if "apps.whop.com" not in u:
                continue
            slug = u.replace("https://", "").replace("/", "_")[:80]
            try:
                (OUT / f"detail__frame__{slug}.html").write_text(f2.content(), encoding="utf-8")
                logger.info(f"saved detail frame: {u}")
            except Exception as e:
                logger.warning(f"could not save frame {u}: {e}")


if __name__ == "__main__":
    main()
