"""MultiPlatformPublisher: posts a single clip to one or more platforms via
the per-platform Playwright publishers, with rule validation gating each
post."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

from db import Repository

from .base import PublishResult
from .instagram_graph import InstagramGraphPublisher
from .rate_limiter import can_post
from .rule_validator import validate as validate_against_rules
from .tiktok_web import TikTokWebPublisher
from .youtube_api import YouTubeAPIPublisher


def _skip_tiktok() -> bool:
    return os.environ.get("SKIP_TIKTOK", "").lower() in ("1", "true", "yes", "on")


class MultiPlatformPublisher:
    """Routes per-platform posting to the safest backend for each:
      - youtube → official YouTube Data API v3 (fully sanctioned)
      - instagram → official Meta Graph API (fully sanctioned)
      - tiktok → Playwright web (no free API approval needed)
    """

    PUBLISHER_CLASSES = {
        "tiktok": TikTokWebPublisher,
        "youtube": YouTubeAPIPublisher,
        "instagram": InstagramGraphPublisher,
    }

    def __init__(self, repo: Optional["Repository"] = None) -> None:
        self.repo = repo or Repository()

    def post_clip(
        self,
        video_path: Path,
        caption: str,
        hashtags: list[str],
        platforms: Iterable[str],
        *,
        duration_sec: float = 0.0,
        campaign_rules: str | None = None,
        platforms_required: list[str] | None = None,
        min_duration_sec: int | None = None,
        max_duration_sec: int | None = None,
    ) -> list[PublishResult]:
        results: list[PublishResult] = []
        full_caption_preview = caption + ("\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags) if hashtags else "")
        for p in platforms:
            pl = p.lower()
            if pl == "tiktok" and _skip_tiktok():
                logger.info("[publisher] tiktok skipped (SKIP_TIKTOK=true)")
                continue
            cls = self.PUBLISHER_CLASSES.get(pl)
            if not cls:
                logger.warning(f"[publisher] unknown platform: {p}")
                continue

            check = validate_against_rules(
                caption=full_caption_preview,
                duration_sec=duration_sec,
                platform=p,
                campaign_rules=campaign_rules,
                platforms_required=platforms_required,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
            )
            if not check.ok:
                logger.error(f"[publisher] {p}: failing rule check: {check.failures}")
                continue
            for w in check.warnings:
                logger.warning(f"[publisher] {p}: {w}")

            # Per-platform daily rate limit — never look robotic.
            rate = can_post(self.repo, p)
            if not rate.ok:
                logger.warning(f"[publisher] {p}: rate-limited — {rate.reason}")
                continue

            try:
                with cls() as pub:
                    r = pub.upload(video_path, caption, hashtags)
                    results.append(r)
                    logger.info(f"[publisher] {p}: posted ({r.post_url or 'url pending'})")
            except Exception as e:
                logger.exception(f"[publisher] {p} failed: {e}")
        return results
