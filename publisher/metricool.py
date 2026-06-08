"""Metricool API client — single publisher for TikTok, YouTube Shorts, and Instagram Reels.

Metricool's public API surface (v2 / v3):
  - Auth via `X-Mc-Auth: <USER_TOKEN>` header
  - Brand-scoped (`blogId` / `brandId` in query string)
  - Upload media → schedule a post that references the media id

This module talks to Metricool with `requests`. We do NOT vendor an
official SDK because Metricool's API has changed in minor ways and the
official Python SDK lags. Endpoints are isolated in this file so they
are easy to swap when the docs shift.

NOTE: The exact endpoints below are based on Metricool's public docs as
of the build. If a call 4xx's, look at the JSON response and adjust the
path / payload — every helper here logs the request + response so debug
is straightforward.

Reference: https://developers.metricool.com/
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests
from loguru import logger

from config import settings

from .base import PublishResult
from .rule_validator import CheckResult, validate as validate_against_rules


METRICOOL_BASE = "https://app.metricool.com/api"

# Platform identifiers Metricool expects in scheduling payloads.
PLATFORM_MAP = {
    "tiktok": "tiktok",
    "youtube": "youtube",
    "instagram": "instagram",
}


class MetricoolError(RuntimeError):
    """Raised when Metricool returns a non-2xx response."""


@dataclass
class MetricoolConfig:
    api_token: str
    brand_id: str
    user_id: Optional[str] = None        # required by some endpoints
    tiktok_account_id: Optional[str] = None
    youtube_account_id: Optional[str] = None
    instagram_account_id: Optional[str] = None


class MetricoolPublisher:
    """Thin synchronous Metricool client."""

    platform = "metricool"

    def __init__(self, cfg: Optional[MetricoolConfig] = None) -> None:
        self.cfg = cfg or MetricoolConfig(
            api_token=settings.metricool_api_token or "",
            brand_id=settings.metricool_brand_id or "",
            user_id=settings.metricool_user_id,
            tiktok_account_id=settings.metricool_tiktok_account_id,
            youtube_account_id=settings.metricool_youtube_account_id,
            instagram_account_id=settings.metricool_instagram_account_id,
        )
        if not self.cfg.api_token or not self.cfg.brand_id:
            raise MetricoolError(
                "Metricool credentials missing — set METRICOOL_API_TOKEN and METRICOOL_BRAND_ID in .env"
            )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "X-Mc-Auth": self.cfg.api_token,
            "Accept": "application/json",
        }

    def _params(self, extra: Optional[dict] = None) -> dict:
        params = {"blogId": self.cfg.brand_id}
        if self.cfg.user_id:
            params["userId"] = self.cfg.user_id
        if extra:
            params.update(extra)
        return params

    def _request(self, method: str, path: str, *, params=None, json=None, files=None, timeout: int = 60) -> dict:
        url = f"{METRICOOL_BASE}{path}"
        merged_params = self._params(params)
        logger.debug(f"[metricool] {method} {url} params={merged_params}")
        try:
            resp = requests.request(
                method,
                url,
                params=merged_params,
                json=json,
                files=files,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise MetricoolError(f"Network error calling Metricool: {e}") from e

        if resp.status_code >= 400:
            raise MetricoolError(
                f"Metricool {resp.status_code} {method} {path}: {resp.text[:500]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    # ------------------------------------------------------------------
    # Media upload
    # ------------------------------------------------------------------
    def upload_media(self, video_path: Path) -> str:
        """Upload a local video to Metricool's media library. Returns the media id."""
        if not video_path.exists():
            raise MetricoolError(f"Video not found: {video_path}")

        with video_path.open("rb") as fh:
            files = {"file": (video_path.name, fh, "video/mp4")}
            data = self._request("POST", "/v2/media/upload", files=files, timeout=300)

        media_id = (
            data.get("id")
            or data.get("mediaId")
            or (data.get("data") or {}).get("id")
        )
        if not media_id:
            raise MetricoolError(f"Metricool upload returned no media id: {data}")
        return str(media_id)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def schedule_post(
        self,
        media_id: str,
        platforms: Iterable[str],
        caption: str,
        hashtags: list[str],
        publish_at: Optional[datetime] = None,
        *,
        duration_sec: Optional[float] = None,
        campaign_rules: Optional[str] = None,
        platforms_required: Optional[list[str]] = None,
        min_duration_sec: Optional[int] = None,
        max_duration_sec: Optional[int] = None,
    ) -> list[PublishResult]:
        """Schedule a single video to one or more platforms.

        publish_at: timezone-aware datetime; if omitted, posts immediately.
        Returns one PublishResult per platform.

        Before scheduling, the proposed caption + clip is validated against
        the campaign's rules (required hashtags, allowed platforms, duration
        limits). Any failure raises MetricoolError so we don't waste an
        upload.
        """
        # Pre-flight: validate against campaign rules per requested platform.
        if campaign_rules or platforms_required or min_duration_sec or max_duration_sec:
            full_caption_preview = caption
            if hashtags:
                full_caption_preview += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)
            for p in platforms:
                check = validate_against_rules(
                    caption=full_caption_preview,
                    duration_sec=duration_sec or 0.0,
                    platform=p,
                    campaign_rules=campaign_rules,
                    platforms_required=platforms_required,
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                )
                if not check.ok:
                    raise MetricoolError(
                        f"Caption/clip fails campaign rules for {p}: {check.failures}"
                    )
                for w in check.warnings:
                    logger.warning(f"[metricool] {p}: {w}")
        if not publish_at:
            publish_at = datetime.now(timezone.utc)
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=timezone.utc)

        full_caption = caption.rstrip()
        if hashtags:
            full_caption = f"{full_caption}\n\n{' '.join('#' + h.lstrip('#') for h in hashtags)}"

        providers = []
        for p in platforms:
            account_id = self._account_id_for(p)
            if not account_id:
                logger.warning(f"[metricool] no account id configured for {p}, skipping")
                continue
            providers.append({
                "network": PLATFORM_MAP[p],
                "accountId": account_id,
            })
        if not providers:
            raise MetricoolError("No Metricool platform accounts configured for any requested platform.")

        payload = {
            "text": full_caption,
            "publicationDate": publish_at.isoformat(),
            "media": [{"id": media_id}],
            "providers": providers,
        }

        data = self._request("POST", "/v2/scheduler/posts", json=payload)

        results: list[PublishResult] = []
        # Metricool returns either a single post or a list keyed by provider.
        raw_posts = data.get("posts") or [data]
        for prov in providers:
            matched = next(
                (p for p in raw_posts if (p.get("network") or "").lower() == prov["network"]),
                raw_posts[0] if raw_posts else {},
            )
            results.append(PublishResult(
                platform=prov["network"],
                platform_post_id=str(matched.get("nativeId") or matched.get("platformPostId") or ""),
                post_url=str(matched.get("permalink") or matched.get("postUrl") or ""),
                metricool_post_id=str(matched.get("id") or matched.get("postId") or ""),
                scheduled_for=publish_at.isoformat(),
                raw=matched,
            ))
        return results

    # ------------------------------------------------------------------
    # Status (used by the tracker to confirm a scheduled post went live)
    # ------------------------------------------------------------------
    def get_post(self, metricool_post_id: str) -> dict:
        return self._request("GET", f"/v2/scheduler/posts/{metricool_post_id}")

    def wait_until_posted(self, metricool_post_id: str, timeout_sec: int = 600, poll_interval: int = 30) -> dict:
        deadline = time.time() + timeout_sec
        last: dict = {}
        while time.time() < deadline:
            last = self.get_post(metricool_post_id)
            status = (last.get("status") or "").lower()
            if status in {"published", "posted", "completed"}:
                return last
            if status in {"failed", "error", "rejected"}:
                raise MetricoolError(f"Metricool post {metricool_post_id} failed: {last}")
            time.sleep(poll_interval)
        raise MetricoolError(f"Timed out waiting for Metricool post {metricool_post_id} to publish: {last}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _account_id_for(self, platform: str) -> Optional[str]:
        return {
            "tiktok": self.cfg.tiktok_account_id,
            "youtube": self.cfg.youtube_account_id,
            "instagram": self.cfg.instagram_account_id,
        }.get(platform)
