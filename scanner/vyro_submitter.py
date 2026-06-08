"""Auto-submit posts to Vyro by pasting the post URL into the Submit
Post modal on the 'Add Clips' page.

The flow (from Chris's screenshots):
  1. Go to https://app.vyro.com/   (or /addclips)
  2. Find the campaign card under "My campaigns".
  3. Click the "Submit post" button on that card.
  4. A modal opens with a "Paste link" input.
  5. Paste the URL into the input.
  6. Click "Submit post" inside the modal.
  7. Modal closes → submission recorded.

This module owns the Playwright session via VyroSession.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from loguru import logger

from scanner.vyro_session import VyroSession


class VyroSubmitter:
    def __init__(self, session: Optional[VyroSession] = None) -> None:
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "VyroSubmitter":
        if self._session is None:
            self._session = VyroSession()
            self._session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> VyroSession:
        if self._session is None:
            raise RuntimeError("VyroSubmitter not started")
        return self._session

    # ------------------------------------------------------------------
    def submit_url_for_campaign(
        self,
        campaign_title_substring: str,
        post_url: str,
    ) -> dict:
        """Open Add Clips page, find campaign, click Submit post, paste URL, submit.
        Returns {ok, error}."""
        page = self.session.page
        try:
            # The "Add Clips" page lists joined campaigns and exposes Submit post.
            page.goto("https://app.vyro.com/", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            # Click the Add Clips tab in the top nav.
            for sel in ('a:has-text("Add Clips")', 'button:has-text("Add Clips")', '[aria-label*="Add Clips"]'):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=1500):
                        loc.click(timeout=3000)
                        break
                except Exception:
                    continue
            time.sleep(3)
        except Exception as e:
            return {"ok": False, "error": f"nav failed: {e}"}

        # Find the campaign card containing the title substring, then its
        # Submit post button.
        card_locator = page.locator(
            f"div:has-text(\"{campaign_title_substring}\")"
        ).first
        try:
            if card_locator.count() == 0:
                return {"ok": False, "error": f"campaign card not found for {campaign_title_substring!r}"}
            # The 'Submit post' button typically sits to the right of the title.
            # Look upward to a container that has both the title AND the button.
            submit_btn = page.locator(
                'button:has-text("Submit post")'
            )
            n_btns = submit_btn.count()
            clicked = False
            for i in range(n_btns):
                btn = submit_btn.nth(i)
                # Check the nearest card-ish ancestor contains our substring.
                try:
                    parent_text = btn.evaluate(
                        "el => { let p = el; for (let i=0; i<6 && p; i++) { if ((p.innerText||'').includes(arguments[0])) return p.innerText; p = p.parentElement; } return ''; }",
                        campaign_title_substring,
                    )
                    if parent_text and campaign_title_substring.lower() in parent_text.lower():
                        btn.click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # Last resort: just click the first Submit post button visible.
                try:
                    submit_btn.first.click(timeout=4000)
                    clicked = True
                except Exception as e:
                    return {"ok": False, "error": f"couldn't click Submit post: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"card lookup failed: {e}"}

        time.sleep(1.5)
        # Modal should be open — fill the URL field.
        url_field = page.locator(
            'input[placeholder*="Paste"], input[placeholder*="link"], input[type="url"]'
        ).first
        try:
            url_field.fill(post_url, timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"couldn't fill URL: {e}"}
        time.sleep(0.6)

        # Click the modal's Submit post button (the second instance now visible).
        try:
            # Find the modal-scoped submit button (the dialog has its own).
            modal_btn = page.locator(
                '[role="dialog"] button:has-text("Submit post"), .modal button:has-text("Submit post")'
            ).first
            if modal_btn.count() > 0:
                modal_btn.click(timeout=4000)
            else:
                # Fallback: re-click whichever Submit post button is now enabled.
                page.locator('button:has-text("Submit post"):not([disabled])').last.click(timeout=4000)
        except Exception as e:
            return {"ok": False, "error": f"modal submit failed: {e}"}

        # Wait for modal to close as a success signal.
        time.sleep(4)
        try:
            modal_present = page.locator('[role="dialog"]').count() > 0
        except Exception:
            modal_present = False
        if modal_present:
            return {"ok": False, "error": "modal still open after submit click"}
        logger.info(f"[vyro-submit] submitted {post_url} to {campaign_title_substring!r}")
        return {"ok": True, "error": None}
