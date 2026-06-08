"""Auto-discover and fetch the campaign brief for a Whop program.

A Whop campaign brief lives in two places:
  1. On the Whop campaign detail page itself (description, rules, etc.)
  2. In an external doc linked from that page — usually a Google Doc, sometimes
     Notion / Dropbox Paper.

This module:
  - Reuses the apps.whop.com warm-up + card-click navigation already proven
    by `top_performers_scraper` and `campaign_scanner`.
  - Extracts the detail-view text.
  - Finds links to known brief hosts (Google Docs in particular).
  - Fetches those docs as plain text via the public `/export?format=txt`
    endpoint — works for any link-shared Google Doc, no auth needed.
  - Returns a `BriefBundle` with on-page + external text concatenated.

The CLI script `scripts/auto_extract_briefs.py` runs this then pipes the
result straight into `engine.rules_extractor` so the campaign row gets
both `campaign_brief` (raw) and `structured_rules` (JSON) populated.
"""

from __future__ import annotations

import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from loguru import logger
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from config import settings


WHOP_BASE = "https://whop.com"
CARD_SELECTOR = ".campaign-card-bg"

# Per-community navigation config. Different Whop communities embed their
# Content Rewards app under different apps.whop.com experience subdomains;
# each one needs both the parent whop.com warm-up URL (so cookies for that
# experience get set) and the direct apps URL we then navigate to.
#
# When Chris joins a new community, add an entry here (or the scanner can
# discover and cache it). The community_id matches `campaigns.community_id`.
COMMUNITY_NAV: dict[str, dict[str, str]] = {
    "clip-farm-official": {
        "warm_url": f"{WHOP_BASE}/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/",
        "apps_url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/experiences/exp_sKCcnfigfcLoSb/browse-campaigns",
    },
}

DEFAULT_COMMUNITY = "clip-farm-official"

GOOGLE_DOC_RE = re.compile(r"https://docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", re.IGNORECASE)
DROPBOX_PAPER_RE = re.compile(r"https://paper\.dropbox\.com/doc/[^\s\"'<>]+", re.IGNORECASE)
NOTION_PUBLIC_RE = re.compile(r"https://(?:www\.)?notion\.so/[^\s\"'<>]+", re.IGNORECASE)

# WeTransfer links indicate source-video locations, not briefs — we record
# them as `source_links` but don't try to read text from them.
WETRANSFER_RE = re.compile(r"https?://(?:www\.|we\.)?(?:wetransfer\.com|tl)/[^\s\"'<>]+", re.IGNORECASE)
DRIVE_FILE_RE = re.compile(r"https://drive\.google\.com/(?:file|drive)/[^\s\"'<>]+", re.IGNORECASE)

DEBUG_DIR = settings.project_root / "data" / "debug" / "fetch_brief"


@dataclass
class BriefBundle:
    on_page_text: str = ""
    external_docs: List[dict] = field(default_factory=list)  # [{url, text}]
    source_links: List[str] = field(default_factory=list)    # WeTransfer / Drive file URLs

    @property
    def full_text(self) -> str:
        parts = [self.on_page_text.strip()]
        for d in self.external_docs:
            parts.append(f"\n\n=== Linked doc: {d['url']} ===\n{d['text'].strip()}")
        return "\n\n".join(p for p in parts if p).strip()


class BriefFetcher:
    def __init__(self, page: Page, debug: bool = True) -> None:
        self.page = page
        self.debug = debug
        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def fetch(self, program_title: str, community_id: Optional[str] = None) -> Optional[BriefBundle]:
        """Navigate to the program's detail page and return the brief bundle.

        `community_id` should match `campaigns.community_id` so we use the
        right Whop community's apps.whop.com experience URL. Defaults to
        Clip Farm if not provided.
        """
        if not self._navigate_to_apps(community_id or DEFAULT_COMMUNITY):
            return None

        card = self._find_card_by_title(program_title)
        if not card:
            logger.error(f"[brief] no program card titled {program_title!r}")
            self._dump("no_card", program_title)
            return None
        logger.info(f"[brief] clicking program card {program_title!r}")
        self._click_card(card)
        time.sleep(10)
        self._dump("after_program_click", program_title)

        on_page = self._extract_visible_text()
        html = self.page.content()
        external_urls = _extract_external_brief_urls(html)
        source_urls = _extract_source_urls(html)

        logger.info(f"[brief] {len(external_urls)} external doc link(s), {len(source_urls)} source link(s)")

        external_docs: list[dict] = []
        for url in external_urls[:5]:  # cap to avoid runaway fetching
            text = _fetch_external_doc_text(url)
            if text and text.strip():
                external_docs.append({"url": url, "text": text})
                logger.info(f"[brief] fetched {len(text)} chars from {url}")
            else:
                logger.warning(f"[brief] could not fetch text from {url}")

        bundle = BriefBundle(
            on_page_text=on_page,
            external_docs=external_docs,
            source_links=source_urls,
        )
        return bundle

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _navigate_to_apps(self, community_id: str) -> bool:
        nav = COMMUNITY_NAV.get(community_id)
        if not nav:
            logger.error(
                f"[brief] no navigation config for community {community_id!r}. "
                f"Add it to scanner.brief_fetcher.COMMUNITY_NAV."
            )
            return False

        warm = nav["warm_url"]
        logger.info(f"[brief] warm-up: {warm}")
        self.page.goto(warm, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(10)

        apps_url = nav["apps_url"]
        logger.info(f"[brief] direct: {apps_url}")
        self.page.goto(apps_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(8)

        self._handle_session_expired()

        try:
            self.page.wait_for_selector(CARD_SELECTOR, timeout=30_000)
        except PWTimeout:
            logger.warning("[brief] cards never rendered")
            self._dump("no_cards")
            return False

        self._kill_overlay()
        return True

    def _handle_session_expired(self) -> None:
        try:
            body = (self.page.locator("body").inner_text(timeout=3_000) or "").lower()
        except Exception:
            return
        if "session expired" in body or "reload page" in body:
            logger.warning("[brief] apps session expired — clicking Reload Page")
            try:
                self.page.locator('button:has-text("Reload Page")').first.click(timeout=5_000)
                time.sleep(8)
            except Exception as e:
                logger.warning(f"[brief] reload click failed: {e}")

    def _kill_overlay(self) -> None:
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
            try:
                h3 = card.locator("h3").first
                if h3.count() == 0:
                    continue
                t = (h3.inner_text(timeout=1_000) or "").strip()
            except Exception:
                continue
            if t.lower() == title.lower():
                return card
        return None

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
                logger.warning(f"[brief] both click and dispatch failed: {e}")

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def _extract_visible_text(self) -> str:
        """Best-effort grab of the rendered text of the campaign detail view.

        We use the body's innerText (which excludes hidden elements) and let
        Claude figure out which lines are the brief vs. chrome.
        """
        try:
            text = self.page.evaluate("() => document.body && document.body.innerText || ''")
            return (text or "")[:40_000]
        except Exception as e:
            logger.warning(f"[brief] visible-text extract failed: {e}")
            return ""

    def _dump(self, label: str, suffix: str = "") -> None:
        if not self.debug:
            return
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", suffix)[:40] if suffix else ""
        stem = f"{label}__{safe}" if safe else label
        try:
            (DEBUG_DIR / f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[brief] dump html failed: {e}")
        try:
            self.page.screenshot(path=str(DEBUG_DIR / f"{stem}.png"), full_page=True)
        except Exception as e:
            logger.warning(f"[brief] screenshot failed: {e}")


# ----------------------------------------------------------------------
# URL helpers
# ----------------------------------------------------------------------
def _extract_external_brief_urls(html: str) -> List[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in GOOGLE_DOC_RE.finditer(html):
        canonical = f"https://docs.google.com/document/d/{m.group(1)}"
        if canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)
    for pat in (DROPBOX_PAPER_RE, NOTION_PUBLIC_RE):
        for m in pat.finditer(html):
            u = m.group(0)
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def _extract_source_urls(html: str) -> List[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for pat in (WETRANSFER_RE, DRIVE_FILE_RE):
        for m in pat.finditer(html):
            u = m.group(0).rstrip(".,;'\"")
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def _fetch_external_doc_text(url: str) -> Optional[str]:
    """Fetch a public/shared external doc as plain text.

    Currently supports Google Docs via the /export?format=txt endpoint
    (works for any link-shared doc with no Google login). Other hosts
    return None for now.
    """
    m = GOOGLE_DOC_RE.match(url)
    if m:
        doc_id = m.group(1)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        return _http_get_text(export_url)
    # Future: Notion / Dropbox Paper share-links could be added here.
    return None


def _http_get_text(url: str, timeout: int = 25) -> Optional[str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning(f"[brief] http fetch failed for {url}: {e}")
        return None
    except Exception as e:
        logger.warning(f"[brief] unexpected fetch error for {url}: {e}")
        return None
