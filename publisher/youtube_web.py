"""Post a Short to YouTube via YouTube Studio web upload.

Studio URL: https://studio.youtube.com/

Flow:
  1. Ensure logged in.
  2. Click the Create button → Upload Videos.
  3. Drop the mp4 file.
  4. Set title + description (description = caption + hashtags).
  5. Click through the four-step wizard:
       - Details
       - Video elements (skip)
       - Checks (skip after they pass)
       - Visibility → Public → Publish
  6. Capture the resulting youtube.com/shorts/... URL from the success
     screen if available.

Stub-level: selectors will need real-run iteration like the Whop scanner.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import TimeoutError as PWTimeout

from .base import PublishResult
from .web_base import PlatformSession


STUDIO_URL = "https://studio.youtube.com/"


class YouTubeWebError(RuntimeError):
    pass


class YouTubeWebPublisher:
    platform = "youtube"

    def __init__(self) -> None:
        self.session = PlatformSession(
            platform="youtube",
            login_url="https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fstudio.youtube.com%2F",
            logged_in_url_hints=("studio.youtube.com",),
        )

    def __enter__(self):
        self.session.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.session.close()

    # ------------------------------------------------------------------
    def upload(self, video_path: Path, caption: str, hashtags: list[str]) -> PublishResult:
        if not video_path.exists():
            raise YouTubeWebError(f"video not found: {video_path}")
        page = self.session.page

        title = caption.split("\n", 1)[0][:95] or video_path.stem
        description = caption
        if hashtags:
            description += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)

        logger.info("[youtube] opening Studio")
        page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        # The Create button uses a known id attribute. If selectors miss,
        # the Studio UI changes will surface here on first real run.
        try:
            page.locator("#create-icon").click(timeout=10_000)
            page.locator("#text-item-0").click(timeout=5_000)  # "Upload videos"
        except Exception as e:
            raise YouTubeWebError(f"could not start upload flow: {e}")
        time.sleep(2)

        try:
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(video_path), timeout=10_000)
        except Exception as e:
            raise YouTubeWebError(f"could not set file input: {e}")
        logger.info(f"[youtube] uploading {video_path.name}")

        # Wait for the title field to be ready.
        try:
            page.wait_for_selector("ytcp-mention-textbox", timeout=180_000)
        except PWTimeout:
            raise YouTubeWebError("title field never appeared")
        time.sleep(2)

        # Title.
        title_input = page.locator("ytcp-mention-textbox").nth(0).locator("#textbox")
        title_input.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        title_input.type(title, delay=10)

        # Description.
        desc_input = page.locator("ytcp-mention-textbox").nth(1).locator("#textbox")
        desc_input.click()
        desc_input.type(description, delay=5)

        # "Not made for kids" — required to advance.
        try:
            page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').click(timeout=5_000)
        except Exception:
            logger.warning("[youtube] could not find made-for-kids radio")

        # Next x3.
        for _ in range(3):
            page.locator("#next-button").click(timeout=10_000)
            time.sleep(1.5)

        # Visibility → Public.
        try:
            page.locator('tp-yt-paper-radio-button[name="PUBLIC"]').click(timeout=5_000)
        except Exception:
            logger.warning("[youtube] could not select Public")
        time.sleep(1)

        # Publish.
        page.locator("#done-button").click(timeout=10_000)

        # Capture the post URL from the success dialog.
        post_url = ""
        try:
            link = page.locator('a[href*="youtu"]').first
            if link.count() > 0:
                post_url = link.get_attribute("href") or ""
        except Exception:
            pass

        return PublishResult(
            platform="youtube",
            platform_post_id="",
            post_url=post_url,
            metricool_post_id="",
            scheduled_for=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw={"title": title},
        )
