"""Pull live view/engagement counts from each platform for posts we've made.

Every post in the DB carries a `platform_post_id` (set by the publisher when
it uploaded). For each platform we know how to ask its API for a stats
snapshot — view count, likes, comments, etc. — and write a row into the
`analytics` table.

We track over time so the brain can compare "what we expected" vs. "what
actually happened" and reweight scoring.

Platform support:
  - YouTube Shorts:  Data API v3 `videos.list?part=statistics` using the
                     existing OAuth credentials (`publisher/youtube_api.py`).
  - Instagram Reels: Graph API `/{media_id}/insights?metric=...` using the
                     long-lived `INSTAGRAM_ACCESS_TOKEN`.
  - TikTok:          deferred until TikTok login is unblocked. The handler
                     returns None so the loop simply skips TikTok posts.

This module is purely "read stats" — it never modifies the post or its URL.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import urllib.error
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from config import settings


@dataclass
class StatsSnapshot:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_sec: Optional[float] = None
    raw: dict = field(default_factory=dict)


class AnalyticsTracker:
    """Per-platform fetchers + one entry point: `fetch_for_post(post_row)`."""

    def __init__(self) -> None:
        self._yt_service = None  # lazy
        self._tt_capture = None  # lazy, reuses persistent profile across calls

    # ------------------------------------------------------------------
    def fetch_for_post(self, post: dict) -> Optional[StatsSnapshot]:
        """Dispatch by platform. Returns None for platforms we can't track yet."""
        platform = (post.get("platform") or "").lower()
        pid = post.get("platform_post_id") or ""
        if not pid:
            logger.warning(f"[analytics] post #{post.get('id')} has no platform_post_id; skipping")
            return None

        if platform == "youtube":
            return self._fetch_youtube(pid)
        if platform == "instagram":
            return self._fetch_instagram(pid)
        if platform == "tiktok":
            return self._fetch_tiktok(pid)
        logger.warning(f"[analytics] unknown platform {platform!r}")
        return None

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------
    def _fetch_youtube(self, video_id: str) -> Optional[StatsSnapshot]:
        service = self._get_youtube_service()
        if service is None:
            return None
        try:
            resp = service.videos().list(
                part="statistics,contentDetails",
                id=video_id,
            ).execute()
        except Exception as e:
            logger.warning(f"[analytics][yt] videos.list failed: {e}")
            return None

        items = resp.get("items") or []
        if not items:
            logger.warning(f"[analytics][yt] no video {video_id} (deleted?)")
            return None
        stats = items[0].get("statistics", {})
        return StatsSnapshot(
            views=int(stats.get("viewCount", 0) or 0),
            likes=int(stats.get("likeCount", 0) or 0),
            comments=int(stats.get("commentCount", 0) or 0),
            raw=items[0],
        )

    def _get_youtube_service(self):
        if self._yt_service is not None:
            return self._yt_service
        try:
            from publisher.youtube_api import YouTubeAPIPublisher
        except Exception as e:
            logger.warning(f"[analytics][yt] cannot import YouTubeAPIPublisher: {e}")
            return None
        try:
            pub = YouTubeAPIPublisher()
            self._yt_service = pub._get_service()
            return self._yt_service
        except Exception as e:
            logger.warning(f"[analytics][yt] OAuth service unavailable: {e}")
            return None

    # ------------------------------------------------------------------
    # Instagram
    # ------------------------------------------------------------------
    # v21 renamed `plays` → `views` and dropped some legacy metrics.
    _IG_METRICS = "views,reach,likes,comments,saved,shares,total_interactions"
    _IG_API_BASE = "https://graph.facebook.com/v21.0"

    def _fetch_instagram(self, media_id: str) -> Optional[StatsSnapshot]:
        token = settings.instagram_access_token
        if not token:
            logger.warning("[analytics][ig] INSTAGRAM_ACCESS_TOKEN missing")
            return None

        params = {
            "metric": self._IG_METRICS,
            "access_token": token,
        }
        url = f"{self._IG_API_BASE}/{media_id}/insights?{urllib.parse.urlencode(params)}"
        data = _http_get_json(url)
        if not data:
            return None
        if "error" in data:
            logger.warning(f"[analytics][ig] insights error: {data['error']}")
            return None

        bucket: dict[str, Any] = {}
        for entry in data.get("data") or []:
            name = entry.get("name")
            values = entry.get("values") or []
            if not name or not values:
                continue
            bucket[name] = values[0].get("value", 0)

        views = int(bucket.get("views") or bucket.get("reach") or 0)
        return StatsSnapshot(
            views=views,
            likes=int(bucket.get("likes") or 0),
            comments=int(bucket.get("comments") or 0),
            shares=int(bucket.get("shares") or 0),
            saves=int(bucket.get("saved") or 0),
            raw=data,
        )


    # ------------------------------------------------------------------
    # TikTok (via TikTok Studio web — no free API)
    # ------------------------------------------------------------------
    def _fetch_tiktok(self, video_id: str) -> Optional[StatsSnapshot]:
        cap = self._get_tt_capture()
        if cap is None:
            return None
        try:
            bucket = cap.read_post_stats(video_id)
        except Exception as e:
            logger.warning(f"[analytics][tt] read_post_stats failed: {e}")
            return None
        if not bucket:
            return None
        return StatsSnapshot(
            views=int(bucket.get("views") or 0),
            likes=int(bucket.get("likes") or 0),
            comments=int(bucket.get("comments") or 0),
            shares=int(bucket.get("shares") or 0),
            saves=int(bucket.get("saves") or 0),
            raw=bucket,
        )

    def _get_tt_capture(self):
        if self._tt_capture is not None:
            return self._tt_capture
        try:
            from scanner.tiktok_studio import TikTokStudioCapture
        except Exception as e:
            logger.warning(f"[analytics][tt] cannot import TikTokStudioCapture: {e}")
            return None
        try:
            cap = TikTokStudioCapture()
            cap.session.start()
            self._tt_capture = cap
            return cap
        except Exception as e:
            logger.warning(f"[analytics][tt] cached profile not available: {e}")
            return None

    def close(self) -> None:
        """Release the cached TikTok session (Playwright browser)."""
        if self._tt_capture is not None:
            try:
                self._tt_capture.session.close()
            except Exception:
                pass
            self._tt_capture = None


# ----------------------------------------------------------------------
def _http_get_json(url: str, timeout: int = 20) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "clipfarmer-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            return json.loads(body)
        except Exception:
            logger.warning(f"[analytics] http {e.code} for {url}")
            return None
    except Exception as e:
        logger.warning(f"[analytics] http error for {url}: {e}")
        return None
