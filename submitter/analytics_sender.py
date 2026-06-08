"""Take 48hr analytics screenshots and DM them to the Whop support chat.

Drives a Playwright browser to:
  1. Open each platform's analytics tab for the posted clip
  2. Screenshot the relevant numbers
  3. Open the Whop community / campaign support chat
  4. Attach + send the screenshots
"""

from __future__ import annotations

from pathlib import Path

from db import Repository
from scanner.whop_login import WhopSession


class AnalyticsSender:
    def __init__(self, session: WhopSession, repo: Repository) -> None:
        self.session = session
        self.repo = repo

    def capture_screenshots(self, post_row) -> list[Path]:
        """Screenshot analytics for one post across platforms. Save to data/screenshots/."""
        raise NotImplementedError

    def send_to_whop(self, campaign_id: int, screenshots: list[Path], message: str) -> None:
        """Open the campaign's support / submissions chat and send the screenshots."""
        raise NotImplementedError

    def process_due_submissions(self) -> int:
        """Find submissions older than the configured threshold without a
        screenshot record, run capture + send, update the row.

        Returns the number of submissions processed.
        """
        raise NotImplementedError
