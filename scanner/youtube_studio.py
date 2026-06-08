"""Capture an authentic YouTube Studio analytics screenshot for a video.

Clip Farm's support workflow specifically asks for a screen recording (or
at minimum a real screenshot) showing the navigation through the Audience
tab — *not* a rendered PNG made from API data. So we open the actual YT
Studio page in a persistent Chrome profile and capture it.

First run: headed browser pops, Chris signs in once → profile saved at
`.auth/youtube-studio-profile/`. Subsequent runs use the cached profile
unattended.

Usage from code:
    from scanner.youtube_studio import YouTubeStudioCapture
    with YouTubeStudioCapture() as cap:
        png = cap.screenshot_audience(video_id="FRf-Xj5SbVE")
        # or a webm recording of the navigation:
        mp4 = cap.record_audience(video_id="FRf-Xj5SbVE", seconds=15)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings
from publisher.web_base import PlatformSession


SCREENSHOTS_DIR = settings.project_root / "data" / "screenshots" / "yt_studio"
RECORDINGS_DIR = settings.project_root / "data" / "screenshots" / "yt_studio_recordings"


class YouTubeStudioCapture:
    """Wraps a PlatformSession pinned to YouTube Studio + analytics navigation."""

    LOGGED_IN_HINTS = ("studio.youtube.com",)

    def __init__(self, headless: Optional[bool] = None) -> None:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.session = PlatformSession(
            platform="youtube-studio",
            login_url="https://studio.youtube.com/",
            logged_in_url_hints=self.LOGGED_IN_HINTS,
            login_url_hints=("/login", "/signin", "accounts.google.com"),
            headless=headless,
            login_wait_seconds=600,
        )

    def __enter__(self) -> "YouTubeStudioCapture":
        self.session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.session.close()

    # ------------------------------------------------------------------
    def screenshot_audience(
        self,
        video_id: str,
        *,
        out_path: Optional[Path] = None,
        wait_seconds: int = 10,
    ) -> Optional[Path]:
        """Navigate to <video>/analytics/tab-audience and save a full-page PNG."""
        page = self.session.page
        url = f"https://studio.youtube.com/video/{video_id}/analytics/tab-audience"
        logger.info(f"[yt-studio] opening {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning(f"[yt-studio] goto failed: {e}")
            return None
        # Let analytics widgets render.
        time.sleep(wait_seconds)
        # Dismiss any onboarding overlays.
        self._dismiss_overlays()

        out_path = out_path or (SCREENSHOTS_DIR / f"yt_studio_audience_{video_id}.png")
        try:
            page.screenshot(path=str(out_path), full_page=True)
        except Exception as e:
            logger.warning(f"[yt-studio] screenshot failed: {e}")
            return None
        size_mb = out_path.stat().st_size / 1_048_576
        logger.info(f"[yt-studio] saved {out_path} ({size_mb:.2f} MB)")
        return out_path

    def screenshot_tab(
        self,
        video_id: str,
        tab: str,  # "overview" | "reach" | "engagement" | "audience"
        *,
        wait_seconds: int = 8,
    ) -> Optional[Path]:
        tabs = {
            "overview": "tab-overview",
            "reach": "tab-reach",
            "engagement": "tab-engagement",
            "audience": "tab-audience",
        }
        path_seg = tabs.get(tab, "tab-audience")
        page = self.session.page
        url = f"https://studio.youtube.com/video/{video_id}/analytics/{path_seg}"
        logger.info(f"[yt-studio] {tab}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.warning(f"[yt-studio] goto failed: {e}")
            return None
        time.sleep(wait_seconds)
        self._dismiss_overlays()
        out = SCREENSHOTS_DIR / f"yt_studio_{tab}_{video_id}.png"
        try:
            page.screenshot(path=str(out), full_page=True)
        except Exception as e:
            logger.warning(f"[yt-studio] screenshot failed: {e}")
            return None
        return out

    def screenshot_all_tabs(self, video_id: str) -> list[Path]:
        """Capture Overview + Reach + Engagement + Audience as four PNGs.

        Useful when Clip Farm support needs the full panel set rather than
        just the audience tab.
        """
        out: list[Path] = []
        for tab in ("overview", "reach", "engagement", "audience"):
            p = self.screenshot_tab(video_id, tab)
            if p:
                out.append(p)
        return out

    # ------------------------------------------------------------------
    def _dismiss_overlays(self) -> None:
        """Close YT Studio's tour / consent banners that block the screenshot."""
        page = self.session.page
        for sel in (
            'button:has-text("Got it")',
            'button:has-text("Dismiss")',
            'button:has-text("Skip")',
            'button[aria-label*="Close"]',
        ):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=800):
                    btn.click(timeout=2_000)
                    time.sleep(0.4)
            except Exception:
                continue
