"""Scrape Vyro's per-campaign Campaign Leaderboard panel.

Each campaign's "Add Clips" page exposes a `Campaign leaderboard` panel
with three tabs: Posts, Views, Earnings. The Views tab is the most
informative — top clippers ranked by views with their actual numbers.

We:
  1. Navigate to the campaign's Add Clips card.
  2. Open the leaderboard popup.
  3. Switch to the Views tab.
  4. Scrape rows: {rank, clipper_handle, views, earnings, posts}.
  5. Optionally drill into individual clipper profiles to grab their
     specific posted clip URLs (Posts tab → per-clipper detail).
  6. Persist to `campaigns.top_performers` so the existing competitor
     pipeline (deep_competitor → Director) consumes it.

This is the "actual competitor data" Chris wanted for active marketplace
campaigns, complementing the YT/TT/IG real-app search.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from loguru import logger

from scanner.vyro_session import VyroSession


class VyroLeaderboardScraper:
    """Pull leaderboard rows from a campaign's Add Clips page."""

    def __init__(self, session: Optional[VyroSession] = None) -> None:
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "VyroLeaderboardScraper":
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
            raise RuntimeError("VyroLeaderboardScraper not started")
        return self._session

    # ------------------------------------------------------------------
    def scrape_leaderboard(self, campaign_title_substring: str) -> list[dict]:
        """Open the Add Clips page, find the campaign card, click its
        Campaign Leaderboard panel, scrape Views-tab rows."""
        page = self.session.page
        try:
            page.goto("https://app.vyro.com/", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            # Click Add Clips nav
            for sel in ('a:has-text("Add Clips")', 'button:has-text("Add Clips")'):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=1500):
                        loc.click(timeout=3000)
                        break
                except Exception:
                    continue
            time.sleep(3)
        except Exception as e:
            logger.warning(f"[vyro-lb] nav failed: {e}")
            return []

        # Find the leaderboard panel inside the campaign's card by traversing
        # from any element containing the title substring.
        try:
            opened = self._open_leaderboard(campaign_title_substring)
            if not opened:
                logger.warning(f"[vyro-lb] couldn't open leaderboard for {campaign_title_substring!r}")
                return []
        except Exception as e:
            logger.warning(f"[vyro-lb] open leaderboard failed: {e}")
            return []

        # Switch to Views tab if not already.
        for view_sel in ('button:has-text("Views")', 'button[role="tab"]:has-text("Views")', '[data-tab="views"]'):
            try:
                tab = page.locator(view_sel).first
                if tab.count() > 0 and tab.is_visible(timeout=1500):
                    tab.click(timeout=2000)
                    time.sleep(1.2)
                    break
            except Exception:
                continue

        # Scrape rows.
        time.sleep(1.5)
        rows = page.evaluate(_LEADERBOARD_HARVEST_JS)
        out: list[dict] = []
        for r in rows:
            entry = _parse_row(r)
            if entry:
                out.append(entry)
        logger.info(f"[vyro-lb] scraped {len(out)} leaderboard row(s) for {campaign_title_substring!r}")
        return out

    def _open_leaderboard(self, title_substring: str) -> bool:
        """Click into the leaderboard panel inside the campaign's card."""
        page = self.session.page
        # Try clicking a "Campaign leaderboard" header within a card that
        # also contains the title substring.
        result = page.evaluate(
            """(title) => {
                const cards = document.querySelectorAll(
                    "section, article, div[class*='card'], div[class*='Card']"
                );
                for (const c of cards) {
                    if (!(c.innerText || "").toLowerCase().includes(title.toLowerCase())) continue;
                    const lb = c.querySelector("*");
                    // Look for "Campaign leaderboard" text inside the card
                    const candidates = c.querySelectorAll("h1, h2, h3, h4, h5, button, a, div");
                    for (const cand of candidates) {
                        const t = (cand.innerText || "").trim().toLowerCase();
                        if (t.includes("campaign leaderboard") || t === "leaderboard") {
                            cand.scrollIntoView();
                            cand.click();
                            return true;
                        }
                    }
                }
                return false;
            }""",
            title_substring,
        )
        if result:
            time.sleep(2)
            return True
        return False


# Page-side: collect rows from whatever leaderboard table is rendered.
_LEADERBOARD_HARVEST_JS = r"""
(() => {
    // Try several layout possibilities — table rows, list items, flex divs.
    const rows = [];
    const containers = document.querySelectorAll(
        "table tbody tr, ul li, ol li, div[class*='row']"
    );
    containers.forEach(el => {
        const t = (el.innerText || "").trim();
        if (!t || t.length > 600) return;
        // Heuristic: a leaderboard row has both a handle (@x) AND a number.
        const hasHandle = /@[A-Za-z0-9_.]+/.test(t);
        const hasNumber = /[0-9][0-9,]*\s*[KMB]?\s*(?:views?|posts?|\$)?/i.test(t);
        if (!hasNumber) return;
        rows.push({
            text: t.slice(0, 600),
            handle: hasHandle ? (t.match(/@[A-Za-z0-9_.]+/) || [""])[0] : "",
        });
    });
    return rows.slice(0, 40);
})()
"""


def _parse_row(r: dict) -> Optional[dict]:
    text = (r.get("text") or "").strip()
    if not text:
        return None
    # Views — first number followed by views or K/M/B.
    views_m = re.search(r"([0-9]+(?:[.,][0-9]+)?\s*[KMB]?)\s*views?", text, re.IGNORECASE)
    views_raw = views_m.group(1).strip() if views_m else None
    if not views_raw:
        # Last-resort: first plain K/M/B number on the line.
        m = re.search(r"\b([0-9]+(?:\.[0-9]+)?[KMB])\b", text)
        views_raw = m.group(1) if m else None
    views = _to_int(views_raw)
    # Earnings — $X.YZ pattern
    earnings_m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", text)
    earnings = float(earnings_m.group(1)) if earnings_m else None
    # Posts — "N posts" pattern
    posts_m = re.search(r"([0-9]+)\s*posts?", text, re.IGNORECASE)
    posts = int(posts_m.group(1)) if posts_m else None
    handle = r.get("handle") or ""
    if not views and not earnings and not handle:
        return None
    return {
        "clipper_handle": handle,
        "views": views,
        "est_earnings": earnings,
        "posts_count": posts,
        "platform": "vyro_leaderboard",
        "source": "vyro_lb",
        "raw_text": text[:300],
    }


def _to_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.upper().replace(",", "").strip()
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return None
    n = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(m.group(2), 1)
    return int(n * mult)
