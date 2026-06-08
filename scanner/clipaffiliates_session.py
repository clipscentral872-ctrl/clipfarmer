"""ClipAffiliates marketplace session + campaign scraper.

Affiliate dashboard at https://www.clipaffiliates.com/affiliate/campaigns.
Same email/password as the burner.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from config import settings
from scanner.marketplace_session import MarketplaceSession


class ClipAffiliatesSession(MarketplaceSession):
    platform = "clipaffiliates"
    login_url = "https://www.clipaffiliates.com/login"
    marketplace_url = "https://www.clipaffiliates.com/affiliate/campaigns"
    logged_in_url_hints = ("/affiliate/", "/dashboard")

    def __init__(self, **kwargs) -> None:
        super().__init__(
            email=kwargs.pop("email", None) or settings.clipaffiliates_email,
            password=kwargs.pop("password", None) or settings.clipaffiliates_password,
            **kwargs,
        )

    def scrape_campaigns(self, limit: int = 50) -> list[dict]:
        from .vyro_session import _CARD_HARVEST_JS, _parse_card
        page = self.page
        try:
            page.goto(self.marketplace_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.warning(f"[clipaffiliates] marketplace nav failed: {e}")
            return []
        time.sleep(3)
        cards = page.evaluate(_CARD_HARVEST_JS)
        out: list[dict] = []
        for c in cards[:limit]:
            entry = _parse_card(c)
            if entry:
                out.append(entry)
        logger.info(f"[clipaffiliates] scraped {len(out)} campaign(s)")
        return out

    def find_campaign(self, name_contains: str) -> Optional[dict]:
        for c in self.scrape_campaigns(limit=100):
            if name_contains.lower() in (c.get("title") or "").lower():
                return c
        return None
