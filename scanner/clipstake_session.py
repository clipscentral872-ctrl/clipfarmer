"""ClipStake marketplace session + campaign scraper.

Dashboard at https://app.clipstake.com/marketplace. Higher CPM than
Clipify ($3-4/1k typical). Same email/password as Discord burner.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from loguru import logger

from config import settings
from scanner.marketplace_session import MarketplaceSession


class ClipStakeSession(MarketplaceSession):
    platform = "clipstake"
    login_url = "https://app.clipstake.com/login"
    marketplace_url = "https://app.clipstake.com/marketplace"
    logged_in_url_hints = ("app.clipstake.com/marketplace", "app.clipstake.com/dashboard")

    def __init__(self, **kwargs) -> None:
        super().__init__(
            email=kwargs.pop("email", None) or settings.clipstake_email,
            password=kwargs.pop("password", None) or settings.clipstake_password,
            **kwargs,
        )

    def scrape_campaigns(self, limit: int = 100) -> list[dict]:
        """ClipStake-specific scraper.

        Cards in the "Available Campaigns" list each have:
          - A title (h2/h3 inside card)
          - A "Details" + "Submit" button pair
          - A CPM badge (top-right, e.g. "$1.00/1k")
          - A "Geo-targeted" badge if region-restricted
          - A progress bar with USDC paid/total
          - A view count + a "Details" link with UUID URL

        We grab cards by detecting the Details/Submit button pair (very
        specific to ClipStake's layout — no other element on the page
        has that combo).
        """
        page = self.page
        try:
            page.goto(self.marketplace_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.warning(f"[clipstake] marketplace nav failed: {e}")
            return []
        time.sleep(3)
        # Scroll to load all cards (ClipStake lazy-loads on scroll)
        last_height = 0
        for _ in range(10):
            height = page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            page.mouse.wheel(0, 4000)
            time.sleep(0.8)
            last_height = height

        cards = page.evaluate(_CLIPSTAKE_HARVEST_JS)
        out: list[dict] = []
        for c in cards[:limit]:
            entry = _parse_clipstake_card(c)
            if entry:
                out.append(entry)
        logger.info(f"[clipstake] scraped {len(out)} campaign(s)")
        return out

    def find_campaign(self, name_contains: str) -> Optional[dict]:
        for c in self.scrape_campaigns(limit=100):
            if name_contains.lower() in (c.get("title") or "").lower():
                return c
        return None


# ClipStake-specific harvester. Targets cards by their Details+Submit button pair.
_CLIPSTAKE_HARVEST_JS = r"""
(() => {
    const out = [];
    // Find every "Details" button or link; its grandparent is the card.
    const detailsButtons = Array.from(document.querySelectorAll("button, a"))
        .filter(el => {
            const t = (el.innerText || "").trim().toLowerCase();
            return t === "details" || t.startsWith("details");
        });
    const seenCards = new Set();
    for (const btn of detailsButtons) {
        // Walk up to find the containing card (has a heading + this button + sibling Submit button)
        let card = btn.parentElement;
        for (let d = 0; d < 8 && card; d++) {
            const hasHeading = !!card.querySelector("h1, h2, h3, h4");
            const hasSubmitBtn = Array.from(card.querySelectorAll("button, a"))
                .some(b => (b.innerText || "").trim().toLowerCase().startsWith("submit"));
            const r = card.getBoundingClientRect();
            if (hasHeading && hasSubmitBtn && r.width > 200 && r.height > 200) {
                if (seenCards.has(card)) break;
                seenCards.add(card);
                // Extract title
                const titleEl = card.querySelector("h1, h2, h3, h4");
                const title = titleEl ? (titleEl.innerText || "").trim() : "";
                // Extract CPM — search for $X/1k or $X-$Y/1k patterns
                const text = card.innerText || "";
                let cpm = null;
                const cpmMatch = text.match(/\$\s*([0-9]+(?:\.[0-9]+)?)(?:\s*-\s*\$\s*([0-9]+(?:\.[0-9]+)?))?\s*\/\s*1\s*k/i);
                if (cpmMatch) {
                    cpm = parseFloat(cpmMatch[1]);
                    if (cpmMatch[2]) {
                        // Range — take the high end
                        cpm = parseFloat(cpmMatch[2]);
                    }
                }
                // Details link URL (for deep-linking later)
                const detailLink = Array.from(card.querySelectorAll("a"))
                    .find(a => (a.innerText || "").trim().toLowerCase().startsWith("details"));
                const detail_url = detailLink ? detailLink.href : null;
                out.push({
                    title: title.slice(0, 200),
                    cpm: cpm,
                    detail_url: detail_url,
                    text: text.slice(0, 800),
                });
                break;
            }
            card = card.parentElement;
        }
    }
    return out;
})()
"""


def _parse_clipstake_card(c: dict) -> Optional[dict]:
    title = (c.get("title") or "").strip()
    if not title or len(title) < 3:
        return None
    return {
        "title": title,
        "cpm_usd": c.get("cpm"),
        "url": c.get("detail_url"),
        "raw_text": c.get("text") or "",
    }
