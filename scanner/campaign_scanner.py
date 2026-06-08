"""Discover Whop Content Rewards campaigns inside Clip Farm.

Clip Farm's Content Rewards app lives in an iframe at:
    https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/experiences/<exp_id>/browse-campaigns

Each campaign is a `<div class="campaign-card-bg ...">` element that contains:
  - <h3>Title</h3>
  - <span>Type</span>            e.g. "Clipping" / "UGC"
  - <span>posted-ago</span>      e.g. "16 hours ago"
  - "<paid_out>$X / $TOTAL"      paid-out vs total budget
  - "<cpm>$Y / 1k views"         payout per 1k views

We parse those values, compute budget_remaining_pct, and upsert each
campaign into the DB. Source video URLs are pulled separately by the
SourceExtractor once we have a per-campaign detail URL.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger
from playwright.sync_api import Frame, Locator, Page, TimeoutError as PWTimeout

from config import settings
from db import Repository

from .whop_login import WhopSession


WHOP_BASE = "https://whop.com"
DEBUG_DIR = settings.project_root / "data" / "debug"

# Known Clip Farm community → Content Rewards app slug mapping.
# Whop renders this section in an iframe whose URL ends with /browse-campaigns.
COMMUNITY_APP_URLS = {
    "clip-farm-official": f"{WHOP_BASE}/joined/clip-farm-official/exp_sKCcnfigfcLoSb/app/",
    "contentrewards":     f"{WHOP_BASE}/joined/contentrewards/discover-campaigns-B5C5S1vijHGVt9/app/",
}

CARD_SELECTOR = ".campaign-card-bg"

MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
CPM_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*1k", re.IGNORECASE)
PAID_OF_TOTAL_RE = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*/\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
)


@dataclass
class DiscoveredCampaign:
    whop_campaign_id: str
    community_id: str
    community_name: str
    title: str
    description: str
    payout_per_1k_views: Optional[float]
    min_duration_sec: Optional[int]
    max_duration_sec: Optional[int]
    platforms_required: list[str]
    rules: str
    submission_url: str
    budget_total: Optional[float] = None
    budget_remaining: Optional[float] = None
    budget_remaining_pct: Optional[float] = None
    min_payout_threshold: Optional[float] = None
    min_views_for_payout: Optional[int] = None
    approval_rate: Optional[float] = None
    campaign_frequency: Optional[str] = None
    viability_score: Optional[float] = None
    ends_at: Optional[str] = None

    def as_db_dict(self) -> dict:
        return asdict(self)


class CampaignScanner:
    def __init__(self, session: WhopSession, repo: Repository, debug: bool = False) -> None:
        self.session = session
        self.repo = repo
        self.debug = debug
        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def scan(self, community_slugs: Iterable[str]) -> list[int]:
        ids: list[int] = []
        for slug in community_slugs:
            try:
                ids.extend(self._scan_community(slug))
            except Exception as e:
                logger.exception(f"[scan] community {slug} failed: {e}")
                self.repo.log_run("scanner", "scan_community", "error", message=f"{slug}: {e}")
        return ids

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _scan_community(self, slug: str) -> list[int]:
        url = COMMUNITY_APP_URLS.get(slug) or f"{WHOP_BASE}/joined/{slug}/"
        page = self.session.page

        logger.info(f"[scan] {slug}: opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        # Whop's apps.whop.com iframe needs a generous fixed delay to load.
        # The debug script proved 20s is enough; networkidle alone isn't.
        logger.info(f"[scan] {slug}: waiting 20s for iframe to populate")
        time.sleep(20)

        for f in page.frames:
            logger.info(f"[scan] frame seen: {f.url}")

        # The campaigns are inside an apps.whop.com iframe. Wait for it.
        frame = self._wait_for_campaigns_frame(page, timeout_sec=30)
        if not frame:
            logger.warning(f"[scan] {slug}: no apps.whop.com frame appeared")
            if self.debug:
                self._dump_page(page, f"no-frame__{slug}")
            return []

        # Wait for at least one campaign card to render inside the frame.
        try:
            frame.wait_for_selector(CARD_SELECTOR, timeout=20_000)
        except PWTimeout:
            logger.warning(f"[scan] {slug}: campaign cards did not render in time")
            if self.debug:
                self._dump_frame(frame, f"no-cards__{slug}")
            return []

        # Lazy lists: scroll the frame to pull more cards if it paginates.
        self._scroll_frame(frame)

        cards = frame.locator(CARD_SELECTOR)
        n = cards.count()
        logger.info(f"[scan] {slug}: {n} campaign card(s) found")

        if self.debug:
            self._dump_frame(frame, f"campaigns__{slug}")

        community_name = "Clip Farm" if slug == "clip-farm-official" else slug
        ids: list[int] = []
        for i in range(n):
            card = cards.nth(i)
            try:
                campaign = self._parse_card(card, slug, community_name, url)
            except Exception as e:
                logger.warning(f"[scan] {slug}: failed to parse card #{i}: {e}")
                continue
            if not campaign:
                continue
            ids.append(self.repo.upsert_campaign(campaign.as_db_dict()))
        return ids

    def _wait_for_campaigns_frame(self, page: Page, timeout_sec: int = 30) -> Optional[Frame]:
        # Prefer the frame whose URL ends with /browse-campaigns. Whop's page
        # may have multiple apps.whop.com iframes (a paywall promo + the
        # real one) and we need the one that actually lists campaigns.
        deadline = time.time() + timeout_sec
        best: Optional[Frame] = None
        while time.time() < deadline:
            for f in page.frames:
                u = f.url or ""
                if "browse-campaigns" in u:
                    logger.info(f"[scan] picked frame: {u}")
                    return f
                if "apps.whop.com" in u and best is None:
                    best = f
            time.sleep(0.5)
        if best:
            logger.info(f"[scan] fallback frame: {best.url}")
        return best

    def _scroll_frame(self, frame: Frame, passes: int = 5) -> None:
        for _ in range(passes):
            try:
                frame.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                break
            time.sleep(1.0)

    def _parse_card(
        self,
        card: Locator,
        community_id: str,
        community_name: str,
        page_url: str,
    ) -> Optional[DiscoveredCampaign]:
        try:
            text = card.inner_text(timeout=2_000) or ""
        except Exception:
            return None
        if not text.strip():
            return None

        title = self._extract_title(card, text)
        type_tag = self._extract_type_tag(card)

        cpm = _extract_cpm(text)
        paid_out, total_budget = _extract_paid_of_total(text)
        budget_remaining = None
        budget_remaining_pct = None
        if paid_out is not None and total_budget and total_budget > 0:
            budget_remaining = round(total_budget - paid_out, 2)
            budget_remaining_pct = round(100.0 * budget_remaining / total_budget, 2)

        # Stable id: community + title (Whop doesn't expose a public id on cards).
        # When we click into a campaign in a future step we'll update this with
        # the real campaign url, but the (community, title) tuple is unique
        # enough for upsert purposes.
        whop_campaign_id = f"{community_id}::{title}"

        return DiscoveredCampaign(
            whop_campaign_id=whop_campaign_id,
            community_id=community_id,
            community_name=community_name,
            title=title,
            description=text[:2_000],
            payout_per_1k_views=cpm,
            min_duration_sec=None,
            max_duration_sec=None,
            platforms_required=[type_tag.lower()] if type_tag else [],
            rules=text,
            submission_url=page_url,
            budget_total=total_budget,
            budget_remaining=budget_remaining,
            budget_remaining_pct=budget_remaining_pct,
            viability_score=_compute_viability_score(
                payout=cpm,
                pct_remaining=budget_remaining_pct,
                min_payout=None,
                min_views=None,
                approval_rate=None,
            ),
        )

    def _extract_title(self, card: Locator, fallback_text: str) -> str:
        try:
            h3 = card.locator("h3").first
            if h3.count() > 0:
                t = (h3.inner_text(timeout=1_000) or "").strip()
                if t:
                    return t[:200]
        except Exception:
            pass
        # fall back to the first line of inner_text
        first_line = fallback_text.split("\n", 1)[0].strip()
        return first_line[:200] or "Untitled"

    def _extract_type_tag(self, card: Locator) -> Optional[str]:
        # The "Clipping" / "UGC" tag is the first inline-flex orange/colored pill.
        try:
            pill = card.locator("span.inline-flex").first
            if pill.count() > 0:
                t = (pill.inner_text(timeout=500) or "").strip()
                if t:
                    return t[:50]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Debug dumpers
    # ------------------------------------------------------------------
    def _dump_page(self, page: Page, label: str) -> None:
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{label}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
        except Exception as e:
            logger.warning(f"[scan][debug] failed to dump {label}: {e}")

    def _dump_frame(self, frame: Frame, label: str) -> None:
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{label}.html").write_text(frame.content(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[scan][debug] failed to dump frame {label}: {e}")


# ----------------------------------------------------------------------
# Pure-text extractors
# ----------------------------------------------------------------------
def _extract_cpm(text: str) -> Optional[float]:
    m = CPM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_paid_of_total(text: str) -> tuple[Optional[float], Optional[float]]:
    m = PAID_OF_TOTAL_RE.search(text)
    if not m:
        return None, None
    try:
        paid = float(m.group(1).replace(",", ""))
        total = float(m.group(2).replace(",", ""))
        return paid, total
    except ValueError:
        return None, None


def _compute_viability_score(
    payout: Optional[float],
    pct_remaining: Optional[float],
    min_payout: Optional[float],
    min_views: Optional[int],
    approval_rate: Optional[float],
) -> Optional[float]:
    if all(v is None for v in (payout, pct_remaining, min_payout, min_views, approval_rate)):
        return None

    budget_component = 0.0
    if pct_remaining is not None:
        budget_component = max(0.0, min(1.0, pct_remaining / 100.0))
        if pct_remaining < 60:
            budget_component *= 0.3

    payout_component = 0.0
    if payout is not None:
        payout_component = min(1.0, payout / 5.0)

    approval_component = approval_rate if approval_rate is not None else 0.5
    reach_component = 1.0

    score = (
        40.0 * budget_component
        + 25.0 * payout_component
        + 20.0 * reach_component
        + 15.0 * approval_component
    )
    return round(score, 2)
