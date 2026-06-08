"""Click into a Clip Farm campaign and dump the submit-form HTML so we
can see what fields exist before writing the auto-filler.

Usage:
    python scripts/debug_submit_form.py <campaign_db_id>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db import Repository
from scanner.whop_login import WhopSession


OUT = Path("data/debug")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/debug_submit_form.py <campaign_db_id>")
        return 2
    cid = int(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)

    repo = Repository()
    with repo.conn() as c:
        row = c.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    if not row:
        print(f"campaign {cid} not found")
        return 2
    campaign = dict(row)
    listings_url = campaign["submission_url"]
    title = campaign["title"]
    print(f"target: #{cid} {title}")
    print(f"listings: {listings_url}")

    # Direct nav to apps.whop.com URL — bypasses Playwright's flaky iframe handling.
    apps_url = "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/experiences/exp_sKCcnfigfcLoSb/browse-campaigns"

    with WhopSession(headless=True) as session:
        page = session.page
        # Warm up the parent page first so apps cookies get established via redirect.
        logger.info(f"warm-up: {listings_url}")
        page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(10)
        logger.info(f"direct goto: {apps_url}")
        page.goto(apps_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(8)

        # Check for "Session Expired" and reload if needed.
        try:
            body_text = (page.locator("body").inner_text(timeout=3_000) or "").lower()
            if "session expired" in body_text or "reload page" in body_text:
                logger.warning("apps session expired — clicking Reload Page")
                page.locator('button:has-text("Reload Page")').first.click(timeout=5_000)
                time.sleep(8)
        except Exception:
            pass

        # Wait for cards directly on the apps page.
        try:
            page.wait_for_selector(".campaign-card-bg", timeout=30_000)
        except Exception as e:
            logger.error(f"cards never rendered: {e}")
            page.screenshot(path=str(OUT / "submit__nocards.png"), full_page=True)
            (OUT / "submit__nocards.html").write_text(page.content(), encoding="utf-8")
            return 1

        # Kill the "Join Our Community" promo overlay that blocks card clicks.
        page.evaluate("""
            (() => {
                document.querySelectorAll('div, section').forEach(el => {
                    const cs = window.getComputedStyle(el);
                    if ((cs.position === 'fixed' || cs.position === 'sticky')
                        && el.offsetWidth > 200
                        && el.offsetHeight > 100
                        && el.innerText
                        && /join our community|join now/i.test(el.innerText)) {
                        el.style.display = 'none';
                    }
                });
            })();
        """)
        time.sleep(1)

        cards = page.locator(".campaign-card-bg")
        frame = page  # use page directly now — no iframe shenanigans
        n = cards.count()
        logger.info(f"{n} cards visible")

        # Find the card matching our title.
        target_card = None
        for i in range(n):
            card = cards.nth(i)
            try:
                h3_text = (card.locator("h3").first.inner_text(timeout=1_000) or "").strip()
            except Exception:
                continue
            if h3_text.lower() == title.lower():
                target_card = card
                logger.info(f"matched card #{i}: {h3_text}")
                break
        if not target_card:
            logger.error(f"no card titled {title!r}")
            return 1

        # Click it and wait for the detail view to render. React SPAs
        # often route on click — log the URL before and after.
        url_before = page.url
        logger.info(f"url before click: {url_before}")
        try:
            target_card.click(timeout=5_000, force=True)
        except Exception:
            target_card.dispatch_event("click")
        logger.info("clicked card, waiting 10s for detail")
        time.sleep(10)
        url_after = page.url
        logger.info(f"url after click:  {url_after}")

        # Save frame state.
        try:
            (OUT / "submit__frame_after_click.html").write_text(frame.content(), encoding="utf-8")
            logger.info("saved frame html after click")
        except Exception as e:
            logger.warning(f"frame dump failed: {e}")

        # Take screenshot of the parent page.
        try:
            page.screenshot(path=str(OUT / "submit__after_click.png"), full_page=True)
            logger.info("saved page screenshot")
        except Exception as e:
            logger.warning(f"screenshot failed: {e}")

        # Try clicking a Submit button if one exists.
        for sel in (
            'button:has-text("Submit")',
            'a:has-text("Submit")',
            '[role="button"]:has-text("Submit")',
        ):
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=2_000):
                    logger.info(f"clicking submit ({sel})")
                    loc.click(timeout=5_000)
                    time.sleep(6)
                    (OUT / "submit__frame_after_submit_btn.html").write_text(frame.content(), encoding="utf-8")
                    page.screenshot(path=str(OUT / "submit__after_submit_btn.png"), full_page=True)
                    logger.info("saved post-submit-button state")
                    break
            except Exception:
                continue

    return 0


if __name__ == "__main__":
    sys.exit(main())
