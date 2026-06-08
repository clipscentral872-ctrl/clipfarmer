"""Auto-submit a posted clip URL to a Whop campaign's submission form.

Driven by Playwright + our cached Whop session. Workflow:
  1. Open the community's content-rewards iframe.
  2. Locate the campaign card by title.
  3. Click it → click Submit.
  4. Fill: Title, Video Link, Demographics Image (Pillow-rendered if none).
  5. Submit.

Returns the submission row id.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import Frame, TimeoutError as PWTimeout

from config import settings
from db import Repository
from scanner.whop_login import WhopSession


class SubmitError(RuntimeError):
    pass


@dataclass
class SubmissionInputs:
    campaign_id: int
    post_id: int
    posted_url: str
    title: str
    demographics_image_path: Optional[Path] = None  # if None, a blank placeholder is generated


class WhopSubmitter:
    def __init__(self, session: WhopSession, repo: Repository) -> None:
        self.session = session
        self.repo = repo

    # ------------------------------------------------------------------
    def submit(self, inputs: SubmissionInputs) -> int:
        page = self.session.page

        # Load the campaign row (we need the listings URL and the campaign title).
        with self.repo.conn() as c:
            row = c.execute(
                "SELECT community_id, title, submission_url FROM campaigns WHERE id = ?",
                (inputs.campaign_id,),
            ).fetchone()
        if not row:
            raise SubmitError(f"campaign {inputs.campaign_id} not found")
        listings_url = row["submission_url"]
        campaign_title = row["title"]

        logger.info(f"[submit] opening listings: {listings_url}")
        page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(22)  # The scanner-proven wait that loads the iframe.

        frame = self._find_campaigns_frame(page)
        if not frame:
            raise SubmitError("apps.whop.com iframe never loaded")

        # Find and click the campaign card matching our title.
        self._click_campaign_card(frame, campaign_title)
        time.sleep(4)

        # Click the Submit button.
        self._click_submit_button(frame)
        time.sleep(2)

        # Fill the form. Title + Video Link are simple inputs; the
        # demographics image is a file input.
        self._fill_form(
            frame,
            title=inputs.title,
            video_url=inputs.posted_url,
            screenshot_path=inputs.demographics_image_path or _generate_blank_screenshot(),
        )
        time.sleep(1)

        # Click the final submit / send button on the form.
        self._click_form_submit(frame)
        time.sleep(3)

        submission_id = self.repo.add_submission(
            post_id=inputs.post_id,
            campaign_id=inputs.campaign_id,
            submitted_url=inputs.posted_url,
        )
        logger.info(f"[submit] submission row {submission_id} created")
        return submission_id

    # ------------------------------------------------------------------
    def _find_campaigns_frame(self, page) -> Optional[Frame]:
        for f in page.frames:
            if "browse-campaigns" in (f.url or "") or "apps.whop.com" in (f.url or ""):
                return f
        return None

    def _click_campaign_card(self, frame: Frame, campaign_title: str) -> None:
        # Match an h3 inside a campaign-card-bg whose text equals our title.
        cards = frame.locator(".campaign-card-bg")
        n = cards.count()
        for i in range(n):
            card = cards.nth(i)
            try:
                title = (card.locator("h3").first.inner_text(timeout=1_000) or "").strip()
            except PWTimeout:
                continue
            if title.lower() == campaign_title.lower():
                logger.info(f"[submit] clicking card #{i}: {title}")
                card.click(timeout=5_000)
                return
        raise SubmitError(f"no campaign card titled {campaign_title!r}")

    def _click_submit_button(self, frame: Frame) -> None:
        for sel in (
            'button:has-text("Submit")',
            'a:has-text("Submit")',
            '[role="button"]:has-text("Submit")',
        ):
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=2_000):
                    loc.click(timeout=5_000)
                    return
            except PWTimeout:
                continue
        raise SubmitError("Submit button not found")

    def _fill_form(
        self,
        frame: Frame,
        title: str,
        video_url: str,
        screenshot_path: Path,
    ) -> None:
        # Title input.
        title_input = frame.locator('input[name*="title" i], input[placeholder*="title" i]').first
        if title_input.count() == 0:
            title_input = frame.locator("input[type='text']").first
        title_input.fill(title, timeout=5_000)

        # Video link input.
        url_input = frame.locator('input[name*="url" i], input[placeholder*="link" i], input[placeholder*="url" i], input[type="url"]').first
        if url_input.count() == 0:
            inputs = frame.locator("input[type='text']")
            if inputs.count() > 1:
                url_input = inputs.nth(1)
        url_input.fill(video_url, timeout=5_000)

        # Demographics image file input.
        file_input = frame.locator('input[type="file"]').first
        if file_input.count() == 0:
            raise SubmitError("Demographics image file input not found")
        file_input.set_input_files(str(screenshot_path), timeout=5_000)

    def _click_form_submit(self, frame: Frame) -> None:
        for sel in (
            'button[type="submit"]:has-text("Submit")',
            'button:has-text("Submit Entry")',
            'button:has-text("Submit Submission")',
            'button:has-text("Submit")',
        ):
            try:
                loc = frame.locator(sel).last
                if loc.count() > 0 and loc.is_visible(timeout=2_000):
                    loc.click(timeout=5_000)
                    return
            except PWTimeout:
                continue
        raise SubmitError("Form submit button not found")


# ----------------------------------------------------------------------
def _generate_blank_screenshot() -> Path:
    """Render a placeholder PNG so the form's file input is satisfied
    even when actual platform analytics aren't ready yet."""
    from PIL import Image, ImageDraw

    out = settings.screenshots_dir / "placeholder-demographics.png"
    if out.exists():
        return out
    img = Image.new("RGB", (1080, 720), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.text((40, 40), "Demographics analytics pending — full screenshot will\nfollow in support chat within 48 hours.", fill=(60, 60, 60))
    img.save(out)
    return out
