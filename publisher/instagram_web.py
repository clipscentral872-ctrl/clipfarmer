"""Post a Reel to Instagram via the web composer.

Instagram only supports Reel uploads on web for Business / Creator
accounts (personal accounts can post, but only on mobile until recently).
We use https://www.instagram.com/?next=%2F where the + button opens the
composer.

Flow:
  1. Ensure logged in.
  2. Click the New Post (+) button.
  3. Pick the mp4 file via the file input.
  4. Click Next (twice — first to skip crop, second to skip filters).
  5. Paste caption.
  6. Click Share.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import TimeoutError as PWTimeout

from .base import PublishResult
from .web_base import PlatformSession


HOME_URL = "https://www.instagram.com/"


class InstagramWebError(RuntimeError):
    pass


class InstagramWebPublisher:
    platform = "instagram"

    def __init__(self) -> None:
        self.session = PlatformSession(
            platform="instagram",
            login_url="https://www.instagram.com/accounts/login/",
            logged_in_url_hints=("instagram.com/?",),
            login_url_hints=("/accounts/login", "/accounts/onetap"),
        )

    def __enter__(self):
        self.session.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.session.close()

    # ------------------------------------------------------------------
    def upload(self, video_path: Path, caption: str, hashtags: list[str]) -> PublishResult:
        if not video_path.exists():
            raise InstagramWebError(f"video not found: {video_path}")
        page = self.session.page

        full_caption = caption.rstrip()
        if hashtags:
            full_caption += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)

        logger.info("[instagram] opening home")
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        # Click the New Post / + button.
        try:
            page.locator('svg[aria-label="New post"]').first.click(timeout=10_000)
            time.sleep(1)
            # Sub-menu pops up; click "Post" (the option to upload from device).
            page.locator('div[role="dialog"] >> text=/Post|Select from computer/i').first.click(timeout=5_000)
        except Exception:
            logger.warning("[instagram] new-post entry path missed, trying file input fallback")

        time.sleep(2)
        try:
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(video_path), timeout=10_000)
        except Exception as e:
            raise InstagramWebError(f"could not set file input: {e}")
        logger.info(f"[instagram] uploading {video_path.name}")
        time.sleep(4)

        # Click Next (crop step).
        for _ in range(2):
            try:
                page.locator('div[role="button"]:has-text("Next")').last.click(timeout=10_000)
                time.sleep(1.5)
            except Exception:
                break

        # Caption textarea.
        try:
            cap = page.locator('textarea[aria-label*="caption" i]').first
            if cap.count() == 0:
                cap = page.locator('div[contenteditable="true"]').first
            cap.click(timeout=5_000)
            cap.type(full_caption, delay=8)
        except Exception as e:
            raise InstagramWebError(f"could not fill caption: {e}")

        # Click Share.
        try:
            page.locator('div[role="button"]:has-text("Share")').last.click(timeout=10_000)
        except Exception as e:
            raise InstagramWebError(f"could not click Share: {e}")

        time.sleep(10)

        return PublishResult(
            platform="instagram",
            platform_post_id="",
            post_url="",
            metricool_post_id="",
            scheduled_for=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw={"caption": full_caption},
        )
