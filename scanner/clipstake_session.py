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
            # `commit` is the fastest wait condition — fires as soon as nav
            # commits, before any resource loads.  The marketplace bundle is
            # heavy and `domcontentloaded` was timing out at 30s on slow links.
            page.goto(self.marketplace_url, wait_until="commit", timeout=60_000)
        except Exception as e:
            logger.warning(f"[clipstake] marketplace nav failed: {e}")
            return []
        time.sleep(6)  # Initial React render + first batch
        # Lazy-load scroll: keep going while either page height OR card count
        # grows.  Stops after 3 consecutive stable iterations (truly done).
        prev_height, prev_n, stable = -1, -1, 0
        for _ in range(40):
            page.mouse.wheel(0, 6000)
            time.sleep(1.0)
            try:
                height = page.evaluate("document.body.scrollHeight")
                n = page.evaluate("document.querySelectorAll('a.MuiCard-root[href^=\"/marketplace/\"]').length")
            except Exception:
                height, n = 0, 0
            if height == prev_height and n == prev_n:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev_height, prev_n = height, n

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


# ClipStake-specific harvester.  As of 2026-06-11 the marketplace is a grid
# of MUI cards, each one an <a href="/marketplace/{uuid}"> containing:
#   - a green chip with the CPM rate (e.g. "3 / 1k views")
#   - an <h6> with the campaign title
#   - body2 paragraphs with the community name and budget like "297/1.7K USDC"
#   - a "<n>K views" total
# Old Details/Submit button pair is gone — they made the whole card clickable.
_CLIPSTAKE_HARVEST_JS = r"""
(() => {
    const out = [];
    const cards = Array.from(document.querySelectorAll('a.MuiCard-root[href^="/marketplace/"]'));
    for (const card of cards) {
        const href = card.getAttribute("href") || "";
        const detail_url = href.startsWith("http") ? href : (location.origin + href);
        const titleEl = card.querySelector("h1,h2,h3,h4,h5,h6");
        const title = titleEl ? (titleEl.innerText || "").trim() : "";
        const text = (card.innerText || "").replace(/\s+/g, " ").trim();

        // CPM: prefer the success-colored MuiChip ("3 / 1k views"), fall back to text scan.
        let cpm = null;
        const cpmChip = card.querySelector(".MuiChip-colorSuccess .MuiChip-label");
        const cpmStr = cpmChip ? (cpmChip.innerText || "").trim() : text;
        const cpmMatch = cpmStr.match(/(\d+(?:\.\d+)?)\s*\/\s*1\s*k/i);
        if (cpmMatch) cpm = parseFloat(cpmMatch[1]);

        // Total views — usually "<n>K views" or "<n>M views" with the tabler-eye icon.
        let views = null;
        const viewsMatch = text.match(/([\d.]+)\s*([KMB])?\s*views/i);
        if (viewsMatch) {
            let n = parseFloat(viewsMatch[1]);
            const unit = (viewsMatch[2] || "").toUpperCase();
            if (unit === "K") n *= 1e3;
            else if (unit === "M") n *= 1e6;
            else if (unit === "B") n *= 1e9;
            views = Math.round(n);
        }

        // Budget: "297/1.7K USDC" → paid 297, total 1700 — useful for budget_remaining_pct.
        let budget_paid = null, budget_total = null;
        const budgetMatch = text.match(/([\d.,]+)\s*\/\s*([\d.,]+)\s*([KMB])?\s*USDC/i);
        if (budgetMatch) {
            const parseAmount = (s, unit) => {
                let n = parseFloat(s.replace(/,/g, ""));
                if ((unit || "").toUpperCase() === "K") n *= 1e3;
                else if ((unit || "").toUpperCase() === "M") n *= 1e6;
                return n;
            };
            // The unit suffix can apply to either side; we accept it on the second.
            budget_paid = parseAmount(budgetMatch[1], null);
            budget_total = parseAmount(budgetMatch[2], budgetMatch[3]);
        }

        out.push({
            title: title.slice(0, 200),
            cpm: cpm,
            detail_url: detail_url,
            text: text.slice(0, 800),
            views: views,
            budget_paid: budget_paid,
            budget_total: budget_total,
        });
    }
    return out;
})()
"""


def _parse_clipstake_card(c: dict) -> Optional[dict]:
    title = (c.get("title") or "").strip()
    if not title or len(title) < 3:
        return None
    budget_total = c.get("budget_total")
    budget_paid = c.get("budget_paid")
    budget_remaining = None
    budget_remaining_pct = None
    if budget_total and budget_total > 0:
        paid = budget_paid or 0
        budget_remaining = max(0.0, budget_total - paid)
        budget_remaining_pct = round(100.0 * budget_remaining / budget_total, 1)
    return {
        "title": title,
        "cpm_usd": c.get("cpm"),
        "url": c.get("detail_url"),
        "raw_text": c.get("text") or "",
        "views": c.get("views"),
        "budget_total": budget_total,
        "budget_remaining": budget_remaining,
        "budget_remaining_pct": budget_remaining_pct,
    }
