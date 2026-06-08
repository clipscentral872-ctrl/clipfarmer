"""Try loading the campaign list page directly via the apps.whop.com URL
to bypass the flaky iframe in the parent page."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.whop_login import WhopSession

DIRECT_URL = "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/experiences/exp_sKCcnfigfcLoSb/browse-campaigns"
PARENT_URL = "https://whop.com/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/"
OUT = Path("data/debug")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with WhopSession(headless=True) as session:
        page = session.page

        # First touch the parent page so auth/cookies for apps.whop.com get set.
        logger.info("warm parent page first")
        page.goto(PARENT_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(5)

        # Now go to the apps URL directly.
        logger.info(f"direct goto: {DIRECT_URL}")
        page.goto(DIRECT_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(6)

        # If we hit a session-expired screen, click Reload Page.
        try:
            body_text = page.locator("body").inner_text(timeout=2_000)
        except Exception:
            body_text = ""
        if "Session Expired" in body_text or "session has expired" in body_text.lower():
            logger.warning("session expired on apps.whop.com — clicking Reload Page")
            try:
                page.get_by_role("button", name="Reload Page").click(timeout=5_000)
            except Exception:
                # fallback: any button containing 'reload'
                page.locator("button:has-text('Reload')").first.click(timeout=5_000)
            time.sleep(10)

        try:
            page.wait_for_selector(".campaign-card-bg", timeout=20_000)
            logger.info("cards present")
        except Exception as e:
            logger.error(f"cards not found: {e}")
            return
        n = page.locator(".campaign-card-bg").count()
        logger.info(f"card count: {n}")

        page.screenshot(path=str(OUT / "direct.png"), full_page=True)
        (OUT / "direct.html").write_text(page.content(), encoding="utf-8")

        # Click into the first card.
        url_before = page.url
        page.locator(".campaign-card-bg").first.click(timeout=5_000)
        logger.info("clicked first card, waiting for navigation")
        time.sleep(6)
        url_after = page.url
        logger.info(f"url before: {url_before}")
        logger.info(f"url after:  {url_after}")

        # Save detail page.
        page.screenshot(path=str(OUT / "direct_detail.png"), full_page=True)
        (OUT / "direct_detail.html").write_text(page.content(), encoding="utf-8")
        logger.info("saved direct_detail.*")


if __name__ == "__main__":
    main()
