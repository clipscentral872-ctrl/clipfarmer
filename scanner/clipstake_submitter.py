"""Auto-submit posts to ClipStake.

ClipStake's submit flow (per Chris's screenshots):
  1. Navigate to a campaign's detail page (UUID URL like
     app.clipstake.com/marketplace/<uuid>)
  2. Click "Start Clipping" button → opens a submit modal/page
  3. Paste the post URL
  4. Confirm submission

Joining a campaign (one-time per campaign before first submit):
  Click "Start Clipping" — same button, triggers join + first submit in one go.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from scanner.clipstake_session import ClipStakeSession


class ClipStakeSubmitter:
    def __init__(self, session: Optional[ClipStakeSession] = None) -> None:
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "ClipStakeSubmitter":
        if self._session is None:
            self._session = ClipStakeSession()
            self._session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> ClipStakeSession:
        if self._session is None:
            raise RuntimeError("ClipStakeSubmitter not started")
        return self._session

    def submit_url_for_campaign(
        self,
        campaign_url_or_title: str,
        post_url: str,
    ) -> dict:
        """Submit a post URL to a ClipStake campaign.

        Accepts EITHER:
          - A campaign detail page URL (app.clipstake.com/marketplace/<uuid>)
          - A title substring (we'll find the card on the marketplace then click in)
        """
        page = self.session.page
        try:
            if campaign_url_or_title.startswith("http"):
                page.goto(campaign_url_or_title, wait_until="domcontentloaded", timeout=30_000)
            else:
                # Marketplace → find card → click Details
                page.goto(self.session.marketplace_url, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                clicked = page.evaluate(
                    """(needle) => {
                        const norm = (s) => (s || "").toLowerCase();
                        const cards = document.querySelectorAll("article, section, div");
                        for (const c of cards) {
                            if (!norm(c.innerText).includes(norm(needle))) continue;
                            // Click the Details link inside
                            const details = Array.from(c.querySelectorAll("a, button"))
                                .find(el => norm(el.innerText).startsWith("details"));
                            if (details) {
                                details.scrollIntoView({block: 'center'});
                                details.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    campaign_url_or_title,
                )
                if not clicked:
                    return {"ok": False, "error": f"couldn't find campaign card for {campaign_url_or_title!r}"}
            time.sleep(2)
        except Exception as e:
            return {"ok": False, "error": f"nav failed: {e}"}

        # Click "Start Clipping" button
        try:
            start_btn = page.locator('button:has-text("Start Clipping")').first
            if start_btn.count() == 0:
                # Maybe already joined — look for "Submit Post" instead
                start_btn = page.locator(
                    'button:has-text("Submit"), button:has-text("Upload Post")'
                ).first
            if start_btn.count() == 0:
                return {"ok": False, "error": "no 'Start Clipping' / 'Submit' button found"}
            start_btn.click(timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"couldn't click Start Clipping: {e}"}

        time.sleep(2)

        # Find the URL input
        url_field = page.locator(
            'input[type="url"], input[placeholder*="link" i], '
            'input[placeholder*="url" i], input[placeholder*="post" i], '
            'input[placeholder*="paste" i]'
        ).first
        try:
            url_field.fill(post_url, timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"couldn't fill URL: {e}"}
        time.sleep(0.6)

        # Click the modal's submit button
        try:
            submit_btn = page.locator(
                '[role="dialog"] button:has-text("Submit"), '
                'button:has-text("Submit Post"):not([disabled]), '
                'button[type="submit"]:has-text("Submit"):not([disabled])'
            ).first
            if submit_btn.count() == 0:
                submit_btn = page.locator('button:has-text("Submit"):not([disabled])').last
            submit_btn.click(timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"modal submit failed: {e}"}

        time.sleep(4)
        logger.info(f"[clipstake] submitted {post_url}")
        return {"ok": True, "error": None}
