"""Auto-submit posts to ClipAffiliates.

ClipAffiliates submit flow (per Chris's screenshots):
  1. Navigate to a campaign's detail page (browse campaigns → click card)
  2. If not joined yet → click "Join Campaign" button
  3. After joining → click "Upload Post" button
  4. Submit Post modal opens → paste link → click "Submit Post"

The submit-window-after-publish requirement is 30 minutes, so we must
fire this immediately after a successful post to YT/IG/TT.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from scanner.clipaffiliates_session import ClipAffiliatesSession


class ClipAffiliatesSubmitter:
    def __init__(self, session: Optional[ClipAffiliatesSession] = None) -> None:
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "ClipAffiliatesSubmitter":
        if self._session is None:
            self._session = ClipAffiliatesSession()
            self._session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> ClipAffiliatesSession:
        if self._session is None:
            raise RuntimeError("ClipAffiliatesSubmitter not started")
        return self._session

    def submit_url_for_campaign(
        self,
        campaign_title_substring: str,
        post_url: str,
    ) -> dict:
        page = self.session.page
        try:
            # Go to My Campaigns first — joined campaigns show there.
            page.goto("https://www.clipaffiliates.com/affiliate/my-campaigns",
                      wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
        except Exception as e:
            return {"ok": False, "error": f"nav failed: {e}"}

        # Find the campaign's card by title substring, click it
        clicked = page.evaluate(
            """(needle) => {
                const norm = (s) => (s || "").toLowerCase();
                const cards = document.querySelectorAll("article, section, div, a");
                for (const c of cards) {
                    if (!norm(c.innerText).includes(norm(needle))) continue;
                    const r = c.getBoundingClientRect();
                    if (r.width < 100 || r.height < 50) continue;
                    c.scrollIntoView({block: 'center'});
                    c.click();
                    return true;
                }
                return false;
            }""",
            campaign_title_substring,
        )
        if not clicked:
            # Not in My Campaigns — try Browse Campaigns and join
            try:
                page.goto("https://www.clipaffiliates.com/affiliate/campaigns",
                          wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                joined = page.evaluate(
                    """(needle) => {
                        const norm = (s) => (s || "").toLowerCase();
                        const cards = document.querySelectorAll("article, section, div");
                        for (const c of cards) {
                            if (!norm(c.innerText).includes(norm(needle))) continue;
                            const joinBtn = Array.from(c.querySelectorAll("button"))
                                .find(b => norm(b.innerText).startsWith("join"));
                            if (joinBtn) {
                                joinBtn.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    campaign_title_substring,
                )
                if not joined:
                    return {"ok": False, "error": f"campaign {campaign_title_substring!r} not found"}
                time.sleep(2)
            except Exception as e:
                return {"ok": False, "error": f"join failed: {e}"}

        # Click "Upload Post" button
        try:
            upload_btn = page.locator(
                'button:has-text("Upload Post"), a:has-text("Upload Post")'
            ).first
            if upload_btn.count() == 0:
                return {"ok": False, "error": "no 'Upload Post' button visible"}
            upload_btn.click(timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"couldn't click Upload Post: {e}"}

        time.sleep(2)

        # Fill the Post Link input
        url_field = page.locator(
            'input[placeholder*="tiktok" i], input[placeholder*="link" i], '
            'input[type="url"]'
        ).first
        try:
            url_field.fill(post_url, timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"couldn't fill URL: {e}"}
        time.sleep(0.5)

        # Click "Submit Post" (the modal's button)
        try:
            submit_btn = page.locator(
                'button:has-text("Submit Post"):not([disabled])'
            ).last
            submit_btn.click(timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"submit failed: {e}"}

        time.sleep(4)
        logger.info(f"[clipaffiliates] submitted {post_url}")
        return {"ok": True, "error": None}
