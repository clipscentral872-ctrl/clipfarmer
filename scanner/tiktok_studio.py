"""Capture per-video analytics from TikTok Studio (web).

TikTok has no free analytics API, so the only way to get reliable
per-video numbers is the logged-in TikTok Studio web UI. Uses the cached
PlatformSession at `.auth/tiktok-profile/`.

Two surfaces:

  - `read_post_stats(video_id)`: scrape views/likes/comments/shares from
    the studio's post detail page. Returns a dict matching StatsSnapshot
    so the existing AnalyticsTracker can pick it up.

  - `screenshot_post_analytics(video_id)`: save a full-page PNG of the
    analytics tab so Whop's 48hr-screenshot requirement is satisfied
    automatically.

We use the studio "content" list as the entry point and look up the
specific post by its TikTok video id (the long numeric one in the URL).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings
from publisher.web_base import PlatformSession


SCREENSHOTS_DIR = settings.project_root / "data" / "screenshots" / "tiktok_studio"
STUDIO_CONTENT_URL = "https://www.tiktok.com/tiktokstudio/content"
# TikTok Studio's per-post analytics URL pattern:
POST_ANALYTICS_URL = "https://www.tiktok.com/tiktokstudio/analytics/post/{video_id}"


# Match human-friendly numbers like "1.2M", "847K", "12,345", "12.5K"
NUM_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMB]?)", re.IGNORECASE)


class TikTokStudioCapture:
    """Cached-session wrapper for TikTok Studio."""

    LOGGED_IN_HINTS = (
        "tiktok.com/tiktokstudio",
        "tiktok.com/foryou",
        "tiktok.com/profile",
    )

    def __init__(self, headless: Optional[bool] = None) -> None:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        self.session = PlatformSession(
            platform="tiktok",
            login_url="https://www.tiktok.com/login",
            logged_in_url_hints=self.LOGGED_IN_HINTS,
            headless=headless,
            login_wait_seconds=600,
        )

    def __enter__(self) -> "TikTokStudioCapture":
        self.session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.session.close()

    # ------------------------------------------------------------------
    def read_post_stats(self, video_id: str, *, wait_seconds: int = 6) -> Optional[dict]:
        """Return {views, likes, comments, shares, saves} or None on failure."""
        page = self.session.page
        url = POST_ANALYTICS_URL.format(video_id=video_id)
        logger.info(f"[tt-studio] opening {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning(f"[tt-studio] goto failed: {e}")
            return None
        time.sleep(wait_seconds)
        self._dismiss_overlays()

        # Walk the rendered DOM and pull metric tiles. TikTok Studio uses
        # an aria-label or label-near-number pattern; we collect every
        # number-with-label pair and filter by keyword.
        candidates = page.evaluate(_METRIC_HARVEST_JS)
        if not candidates:
            logger.warning(f"[tt-studio] no metrics found on {url}")
            return None

        bucket: dict[str, int] = {}
        for entry in candidates:
            label = (entry.get("label") or "").lower()
            raw = (entry.get("value") or "").strip()
            n = _parse_number(raw)
            if n is None:
                continue
            for key, hints in _METRIC_KEYS.items():
                if any(h in label for h in hints) and key not in bucket:
                    bucket[key] = n
                    break

        if not bucket:
            logger.warning(f"[tt-studio] no labelled metrics matched for {video_id}")
            return None

        return {
            "views": bucket.get("views", 0),
            "likes": bucket.get("likes", 0),
            "comments": bucket.get("comments", 0),
            "shares": bucket.get("shares", 0),
            "saves": bucket.get("saves", 0),
        }

    def screenshot_post_analytics(
        self,
        video_id: str,
        *,
        out_path: Optional[Path] = None,
        wait_seconds: int = 10,
    ) -> Optional[Path]:
        """Save the post's analytics page as a full-page PNG."""
        page = self.session.page
        url = POST_ANALYTICS_URL.format(video_id=video_id)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning(f"[tt-studio] goto failed: {e}")
            return None
        time.sleep(wait_seconds)
        self._dismiss_overlays()

        out_path = out_path or (SCREENSHOTS_DIR / f"tiktok_analytics_{video_id}.png")
        try:
            page.screenshot(path=str(out_path), full_page=True)
        except Exception as e:
            logger.warning(f"[tt-studio] screenshot failed: {e}")
            return None
        logger.info(f"[tt-studio] saved {out_path}")
        return out_path

    # ------------------------------------------------------------------
    def _dismiss_overlays(self) -> None:
        page = self.session.page
        for sel in (
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            'button[aria-label*="Close"]',
        ):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=800):
                    btn.click(timeout=2_000)
                    time.sleep(0.3)
            except Exception:
                continue


# ----------------------------------------------------------------------
_METRIC_KEYS = {
    "views":    ("views", "video views", "play"),
    "likes":    ("likes",),
    "comments": ("comments",),
    "shares":   ("shares",),
    "saves":    ("saves", "saved"),
}


def _parse_number(s: str) -> Optional[int]:
    if not s:
        return None
    m = NUM_RE.match(s.strip().replace(",", ""))
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    suf = m.group(2).upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suf, 1)
    return int(n * mult)


# ----------------------------------------------------------------------
# Page-side harvester: walk DOM, return list of {label, value}.
# Heuristic: any small element whose text is a number, pair with its
# nearest preceding/following text node of <=30 chars.
# ----------------------------------------------------------------------
_METRIC_HARVEST_JS = r"""
(() => {
    const isNumberish = t => /^[0-9][0-9,.]*\s*[KMB]?$/i.test((t || "").trim());
    const trimmed = el => (el.innerText || "").trim();
    const out = [];
    const seen = new Set();
    document.querySelectorAll("div, span, strong, p").forEach(el => {
        const t = trimmed(el);
        if (!isNumberish(t) || t.length > 12) return;
        if (seen.has(el)) return;
        seen.add(el);
        // Find a nearby label: walk up + check siblings for short text.
        let label = "";
        let parent = el.parentElement;
        for (let d = 0; d < 3 && parent; d++) {
            // Direct text in siblings
            for (const sib of parent.children) {
                if (sib === el) continue;
                const st = trimmed(sib);
                if (st && st.length <= 40 && !isNumberish(st)) {
                    label = st;
                    break;
                }
            }
            if (label) break;
            parent = parent.parentElement;
        }
        out.push({label, value: t});
    });
    return out;
})()
"""
