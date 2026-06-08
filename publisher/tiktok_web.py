"""Post a clip to TikTok via the web upload page.

Reliable URL: https://www.tiktok.com/tiktokstudio/upload?from=upload

Flow:
  1. Ensure logged in (cached or interactive first-run).
  2. Navigate to the upload page.
  3. Drop the mp4 file into the file input.
  4. Wait for upload progress to finish.
  5. Fill caption into the contenteditable.
  6. Click Post.
  7. Capture the resulting post URL (sometimes requires polling).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import TimeoutError as PWTimeout

from .base import PublishResult
from .web_base import PlatformSession


UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload"
LOGGED_IN_PROBE = "https://www.tiktok.com/foryou"


class TikTokWebError(RuntimeError):
    pass


class TikTokWebPublisher:
    platform = "tiktok"

    def __init__(self) -> None:
        self.session = PlatformSession(
            platform="tiktok",
            login_url="https://www.tiktok.com/login",
            logged_in_url_hints=(LOGGED_IN_PROBE, "/foryou", "/profile"),
        )

    def __enter__(self):
        self.session.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.session.close()

    # ------------------------------------------------------------------
    def upload(self, video_path: Path, caption: str, hashtags: list[str]) -> PublishResult:
        if not video_path.exists():
            raise TikTokWebError(f"video not found: {video_path}")
        page = self.session.page

        full_caption = caption.rstrip()
        if hashtags:
            full_caption += "\n" + " ".join("#" + h.lstrip("#") for h in hashtags)

        logger.info(f"[tiktok] opening upload page")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        # 1. Upload the file.
        file_input = page.locator('input[type="file"]').first
        try:
            file_input.set_input_files(str(video_path), timeout=10_000)
            logger.info(f"[tiktok] set file: {video_path.name}")
        except Exception as e:
            raise TikTokWebError(f"could not set file input: {e}")

        # 2. Wait for processing — look for the caption area to be ready.
        try:
            page.wait_for_selector('div[contenteditable="true"]', timeout=120_000)
        except PWTimeout:
            raise TikTokWebError("caption editor never appeared after upload")
        time.sleep(2)

        # 3. Fill caption. TikTok uses a contenteditable, so click + type
        # rather than .fill().
        editor = page.locator('div[contenteditable="true"]').first
        editor.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        editor.type(full_caption, delay=10)

        # 4. Wait for the Post button to become enabled.
        post_btn = page.locator('button:has-text("Post")').last
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if post_btn.is_enabled(timeout=1_000):
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise TikTokWebError("Post button never became enabled")

        logger.info("[tiktok] clicking Post")
        post_btn.click()

        # 5. Wait for success — TikTok redirects to /tiktokstudio/content or shows a toast.
        time.sleep(8)
        try:
            page.wait_for_url("**/tiktokstudio/content**", timeout=60_000)
        except PWTimeout:
            logger.warning("[tiktok] did not see content redirect; assuming success")

        # TikTok's web flow doesn't give us the public URL directly. We
        # leave platform_post_id empty for now; the tracker can resolve it
        # later by visiting /studio/content and matching the most recent
        # upload by caption.
        return PublishResult(
            platform="tiktok",
            platform_post_id="",
            post_url="",
            metricool_post_id="",
            scheduled_for=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw={"caption": full_caption},
        )
