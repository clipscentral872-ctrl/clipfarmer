"""Scrape the Top Performing Videos section from a Whop sub-campaign page.

Navigation flow (proven by existing scanner + debug_submit_form):
  1. Warm-up: goto whop.com/joined/.../app/ so apps cookies are set.
  2. Direct goto: apps.whop.com/.../browse-campaigns
  3. If "Session Expired" → click Reload Page.
  4. Wait for .campaign-card-bg to render.
  5. Kill the "Join Our Community" promo overlay.
  6. Click the program card matching our campaign title (current scanner
     stores programs as 'campaigns', so the title we have is the program).
  7. The detail view should list one or more sub-campaigns. We pick either
     (a) the one whose name the caller passed, or (b) the first one.
  8. Click into the sub-campaign.
  9. Find the "Top Performing Videos" section and extract video URLs +
     view counts + any visible titles.

This is best-effort: Whop's UI changes, and the SPA may render any number
of intermediate states. So we ALWAYS dump HTML + screenshots to
`data/debug/scrape_top/` — even on success — so future runs can iterate.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from config import settings


WHOP_BASE = "https://whop.com"
APPS_BROWSE_URL = "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/experiences/exp_sKCcnfigfcLoSb/browse-campaigns"
CARD_SELECTOR = ".campaign-card-bg"

# Matches "1.2M views", "847K", "12,345 views", etc.
VIEWS_RE = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?\s*[KMB]?)\s*(?:views|view)?\b", re.IGNORECASE)
EARNINGS_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")

VIDEO_URL_PATTERNS = [
    re.compile(r"https?://(?:www\.)?tiktok\.com/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?youtube\.com/(?:shorts|watch)[^\s\"'<>]*", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?youtu\.be/[^\s\"'<>]+", re.IGNORECASE),
]


DEBUG_DIR = settings.project_root / "data" / "debug" / "scrape_top"


@dataclass
class TopPerformer:
    title: Optional[str] = None
    views: Optional[str] = None       # human-friendly string ("1.2M")
    est_earnings: Optional[float] = None
    platform: Optional[str] = None
    url: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class TopPerformersScraper:
    def __init__(self, page: Page, debug: bool = True) -> None:
        self.page = page
        self.debug = debug
        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def scrape(self, program_title: str, sub_campaign_title: Optional[str] = None) -> List[TopPerformer]:
        """Navigate to the sub-campaign and return any top performers found."""
        self._navigate_to_apps()

        program_card = self._find_card_by_title(program_title)
        if not program_card:
            logger.error(f"[scrape] no program card titled {program_title!r}")
            self._dump("no_program_card")
            return []
        logger.info(f"[scrape] clicking program card {program_title!r}")
        self._click_card(program_card)
        time.sleep(8)
        self._dump("after_program_click")

        sub_card = self._find_sub_campaign_card(sub_campaign_title)
        if sub_card is None:
            logger.error(f"[scrape] no sub-campaign card found (looking for {sub_campaign_title!r})")
            return []
        sub_title = self._card_title(sub_card)
        logger.info(f"[scrape] clicking sub-campaign card {sub_title!r}")
        self._click_card(sub_card)
        time.sleep(8)
        self._dump("after_subcampaign_click")

        performers = self._extract_top_performers()
        logger.info(f"[scrape] extracted {len(performers)} top performer(s)")
        return performers

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _navigate_to_apps(self) -> None:
        # Warm-up to establish apps.whop.com cookies via the parent page.
        warm = f"{WHOP_BASE}/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/"
        logger.info(f"[scrape] warm-up: {warm}")
        self.page.goto(warm, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(10)

        logger.info(f"[scrape] direct: {APPS_BROWSE_URL}")
        self.page.goto(APPS_BROWSE_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(8)

        self._handle_session_expired()
        try:
            self.page.wait_for_selector(CARD_SELECTOR, timeout=30_000)
        except PWTimeout:
            logger.warning("[scrape] cards never rendered")
            self._dump("no_cards_on_apps")
            return

        self._kill_overlay()

    def _handle_session_expired(self) -> None:
        try:
            body = (self.page.locator("body").inner_text(timeout=3_000) or "").lower()
        except Exception:
            return
        if "session expired" in body or "reload page" in body:
            logger.warning("[scrape] apps session expired — clicking Reload Page")
            try:
                self.page.locator('button:has-text("Reload Page")').first.click(timeout=5_000)
                time.sleep(8)
            except Exception as e:
                logger.warning(f"[scrape] reload click failed: {e}")

    def _kill_overlay(self) -> None:
        # The "Join Our Community" promo overlay intercepts card clicks.
        self.page.evaluate("""
            (() => {
                document.querySelectorAll('div, section').forEach(el => {
                    const cs = window.getComputedStyle(el);
                    if ((cs.position === 'fixed' || cs.position === 'sticky')
                        && el.offsetWidth > 200 && el.offsetHeight > 100
                        && el.innerText
                        && /join our community|join now/i.test(el.innerText)) {
                        el.style.display = 'none';
                    }
                });
            })();
        """)
        time.sleep(0.5)

    def _find_card_by_title(self, title: str) -> Optional[Locator]:
        cards = self.page.locator(CARD_SELECTOR)
        n = cards.count()
        for i in range(n):
            card = cards.nth(i)
            t = self._card_title(card)
            if t and t.lower() == title.lower():
                return card
        return None

    def _find_sub_campaign_card(self, preferred_title: Optional[str]) -> Optional[Locator]:
        # After clicking a program, sub-campaigns may render as the same
        # .campaign-card-bg cards OR as something different. Try the same
        # selector first; fall back to "any card-looking element".
        cards = self.page.locator(CARD_SELECTOR)
        n = cards.count()
        if n == 0:
            logger.warning("[scrape] no campaign-card-bg cards after program click; the sub-campaign list may use a different selector — see dumped HTML")
            return None

        if preferred_title:
            for i in range(n):
                card = cards.nth(i)
                t = self._card_title(card)
                if t and preferred_title.lower() in t.lower():
                    return card
            logger.info(f"[scrape] no sub-campaign matched {preferred_title!r}; falling back to first")

        return cards.first

    def _click_card(self, card: Locator) -> None:
        try:
            card.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        try:
            card.click(timeout=5_000, force=True)
        except Exception:
            try:
                card.dispatch_event("click")
            except Exception as e:
                logger.warning(f"[scrape] both click and dispatch failed: {e}")

    def _card_title(self, card: Locator) -> str:
        try:
            h3 = card.locator("h3").first
            if h3.count() > 0:
                return (h3.inner_text(timeout=1_000) or "").strip()
        except Exception:
            pass
        try:
            return (card.inner_text(timeout=1_000) or "").split("\n", 1)[0].strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def _extract_top_performers(self) -> List[TopPerformer]:
        """Find the Top Performing section and pull what we can.

        Strategy: locate an element whose text contains 'Top Performing',
        walk up to a likely container, then scan child links/cards for
        view counts + video URLs.
        """
        html = self.page.content()
        # Save full HTML always — extraction is iterative.
        self._dump("subcampaign_html", html)

        # Use JS to find the Top Performing section's bounding container and
        # return a list of {text, href, src} per visible candidate child.
        candidates = self.page.evaluate(_TOP_PERFORMERS_JS)
        if not candidates:
            logger.warning("[scrape] page-level JS extraction returned 0 candidates")
            return self._fallback_text_extract(html)

        performers: List[TopPerformer] = []
        for c in candidates:
            tp = _parse_candidate(c)
            if tp:
                performers.append(tp)
        if not performers:
            logger.info("[scrape] candidates found but none parsed cleanly; trying regex fallback")
            return self._fallback_text_extract(html)
        return performers

    def _fallback_text_extract(self, html: str) -> List[TopPerformer]:
        """Last-resort: scan the whole page text for view counts and video URLs.

        Useful when our 'Top Performing' container heuristic misses but the
        underlying URLs are still in the DOM.
        """
        urls: list[str] = []
        for pat in VIDEO_URL_PATTERNS:
            urls.extend(pat.findall(html))
        urls = list(dict.fromkeys(urls))  # de-dup, preserve order
        if not urls:
            return []
        logger.info(f"[scrape] fallback found {len(urls)} candidate video URL(s)")
        out = []
        for u in urls[:20]:
            out.append(TopPerformer(url=u, platform=_platform_from_url(u)))
        return out

    # ------------------------------------------------------------------
    def _dump(self, label: str, html: Optional[str] = None) -> None:
        if not self.debug:
            return
        try:
            (DEBUG_DIR / f"{label}.html").write_text(html or self.page.content(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[scrape] dump html failed: {e}")
        try:
            self.page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
        except Exception as e:
            logger.warning(f"[scrape] screenshot failed: {e}")


# ----------------------------------------------------------------------
# Browser-side helper: find Top Performing section and emit candidates.
# ----------------------------------------------------------------------
_TOP_PERFORMERS_JS = r"""
(() => {
    // Find the first text node containing "Top Performing".
    const all = document.querySelectorAll("h1, h2, h3, h4, h5, h6, div, span, p");
    let header = null;
    for (const el of all) {
        const t = (el.innerText || "").trim();
        if (/top performing/i.test(t) && t.length < 100) {
            header = el;
            break;
        }
    }
    if (!header) return [];

    // Walk up until we find a container with multiple children that look
    // like cards (anchor tags or grid items).
    let container = header.parentElement;
    for (let i = 0; i < 6 && container; i++) {
        const links = container.querySelectorAll("a[href], img, video, [data-video], [class*='card']");
        if (links.length >= 2) break;
        container = container.parentElement;
    }
    if (!container) return [];

    // Each card candidate: an <a> ancestor of an img/video, or a direct child
    // div containing a thumbnail. Collect text + href + src.
    const cards = [];
    container.querySelectorAll("a[href]").forEach(a => {
        const r = a.getBoundingClientRect();
        if (r.width < 40 || r.height < 40) return;
        const img = a.querySelector("img, video");
        cards.push({
            text: (a.innerText || "").trim(),
            href: a.href || null,
            src: img ? (img.src || img.poster || null) : null,
            tag: "a"
        });
    });

    // If we got nothing with anchors, fall back to grid-like child divs.
    if (cards.length === 0) {
        const kids = Array.from(container.children);
        for (const k of kids) {
            const r = k.getBoundingClientRect();
            if (r.width < 80 || r.height < 80) continue;
            const img = k.querySelector("img, video");
            cards.push({
                text: (k.innerText || "").trim(),
                href: null,
                src: img ? (img.src || img.poster || null) : null,
                tag: k.tagName.toLowerCase()
            });
        }
    }
    return cards;
})();
"""


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------
def _parse_candidate(c: dict) -> Optional[TopPerformer]:
    text = (c.get("text") or "").strip()
    href = c.get("href") or None

    views = _first_views(text)
    earnings = _first_earnings(text)
    url = href if href and any(p.match(href) for p in VIDEO_URL_PATTERNS) else None
    platform = _platform_from_url(url) if url else None

    # Title: the first short non-numeric line.
    title = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if VIEWS_RE.fullmatch(line) or EARNINGS_RE.fullmatch(line):
            continue
        if len(line) > 8 and not line.startswith("$"):
            title = line[:200]
            break

    if not any([views, earnings, url, title]):
        return None
    return TopPerformer(
        title=title,
        views=views,
        est_earnings=earnings,
        platform=platform,
        url=url,
        notes=text[:300] if text else None,
    )


def _first_views(text: str) -> Optional[str]:
    for line in text.splitlines():
        m = VIEWS_RE.search(line)
        if m and re.search(r"view", line, re.IGNORECASE):
            return m.group(1).strip()
    # No explicit "views" word — pick the first number with K/M/B suffix as best guess.
    m = re.search(r"\b([0-9]+(?:\.[0-9]+)?[KMB])\b", text)
    return m.group(1) if m else None


def _first_earnings(text: str) -> Optional[float]:
    m = EARNINGS_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _platform_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return None
