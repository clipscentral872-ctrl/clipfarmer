"""Use the parent-page + 20s sleep approach (which proved reliable in
scanner-v4) and then click into a campaign via the iframe."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.whop_login import WhopSession

PARENT_URL = "https://whop.com/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/"
OUT = Path("data/debug")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with WhopSession(headless=True) as session:
        page = session.page
        logger.info("goto parent")
        page.goto(PARENT_URL, wait_until="domcontentloaded", timeout=30_000)
        logger.info("sleeping 22s for iframe to populate")
        time.sleep(22)

        # Find the apps frame.
        frame = None
        for f in page.frames:
            if "apps.whop.com" in (f.url or ""):
                frame = f
                logger.info(f"apps frame: {f.url}")
                break
        if not frame:
            logger.error("no apps frame")
            for f in page.frames:
                logger.info(f"  saw: {f.url}")
            return

        # Wait for cards inside the frame.
        try:
            frame.wait_for_selector(".campaign-card-bg", timeout=20_000)
        except Exception as e:
            logger.error(f"no cards: {e}")
            return
        cards = frame.locator(".campaign-card-bg")
        n = cards.count()
        logger.info(f"{n} cards")

        # Click first card.
        before_parent_url = page.url
        before_frame_url = frame.url
        cards.nth(0).click(timeout=5_000)
        logger.info("clicked card; waiting 8s")
        time.sleep(8)

        # Did the parent navigate? Did the frame navigate?
        logger.info(f"parent url before: {before_parent_url}")
        logger.info(f"parent url after:  {page.url}")
        logger.info(f"frame url before:  {before_frame_url}")
        try:
            logger.info(f"frame url after:   {frame.url}")
        except Exception as e:
            logger.info(f"frame stale: {e}")

        # Save state.
        page.screenshot(path=str(OUT / "click_v2_parent.png"), full_page=True)
        (OUT / "click_v2_parent.html").write_text(page.content(), encoding="utf-8")
        # Try to capture each apps frame.
        for f in page.frames:
            u = f.url or ""
            if "apps.whop.com" not in u:
                continue
            try:
                html = f.content()
                slug = u.replace("https://", "").replace("/", "_")[:80]
                (OUT / f"click_v2_frame__{slug}.html").write_text(html, encoding="utf-8")
                logger.info(f"saved frame: {u} ({len(html):,} chars)")
            except Exception as e:
                logger.warning(f"could not save {u}: {e}")


if __name__ == "__main__":
    main()
