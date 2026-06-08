"""Fetch detailed analytics for a YouTube post.

Uses the same OAuth credentials as the YouTube uploader (`publisher/
youtube_api.py`) — we just added the `youtube.readonly` and
`yt-analytics.readonly` scopes so we can pull view-count, country
breakdown, age + gender demographics, and lifetime watch time.

Output is a dict that's both Telegram-friendly (rendered as text) and
PNG-friendly (passed to `render_analytics_png` for the 48hr screenshot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class YouTubeAnalyticsSnapshot:
    video_id: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    watch_time_minutes: float = 0.0
    duration_seconds: float = 0.0
    posted_at: Optional[str] = None
    title: Optional[str] = None
    countries: list[tuple[str, int]] = field(default_factory=list)         # [(country_code, views)]
    age_gender: list[tuple[str, str, float]] = field(default_factory=list) # [(ageGroup, gender, percent)]
    raw: dict = field(default_factory=dict)

    def top_countries(self, n: int = 5) -> list[tuple[str, int]]:
        return sorted(self.countries, key=lambda r: r[1], reverse=True)[:n]


# ----------------------------------------------------------------------
def fetch_for_video(video_id: str) -> Optional[YouTubeAnalyticsSnapshot]:
    """Return a snapshot for a single YouTube video id, or None on auth failure."""
    services = _get_services()
    if not services:
        return None
    data_service, analytics_service = services

    snap = YouTubeAnalyticsSnapshot(video_id=video_id)

    # --- Basic stats from Data API v3 (cheap, public-style read). ----------
    try:
        resp = data_service.videos().list(
            part="snippet,statistics,contentDetails", id=video_id,
        ).execute()
        items = resp.get("items") or []
        if items:
            stats = items[0].get("statistics", {})
            snippet = items[0].get("snippet", {})
            content = items[0].get("contentDetails", {})
            snap.views = int(stats.get("viewCount", 0) or 0)
            snap.likes = int(stats.get("likeCount", 0) or 0)
            snap.comments = int(stats.get("commentCount", 0) or 0)
            snap.title = snippet.get("title")
            snap.posted_at = snippet.get("publishedAt")
            snap.duration_seconds = _parse_iso8601_duration(content.get("duration") or "")
            snap.raw["data"] = items[0]
    except Exception as e:
        logger.warning(f"[yt-analytics] videos.list failed: {e}")

    # --- Demographics from YouTube Analytics API. -------------------------
    # These calls work for OUR videos under the auth'd channel only.
    filter_str = f"video=={video_id}"
    end_date = "today"
    start_date = "2020-01-01"   # safely covers since the video was posted

    try:
        country_resp = analytics_service.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views",
            dimensions="country",
            filters=filter_str,
            sort="-views",
        ).execute()
        snap.countries = [
            (row[0], int(row[1])) for row in (country_resp.get("rows") or [])
        ]
        snap.raw["countries"] = country_resp
    except Exception as e:
        logger.warning(f"[yt-analytics] country report failed: {e}")

    try:
        ag_resp = analytics_service.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="viewerPercentage",
            dimensions="ageGroup,gender",
            filters=filter_str,
        ).execute()
        snap.age_gender = [
            (row[0], row[1], float(row[2])) for row in (ag_resp.get("rows") or [])
        ]
        snap.raw["age_gender"] = ag_resp
    except Exception as e:
        logger.warning(f"[yt-analytics] age/gender report failed: {e}")

    try:
        wt_resp = analytics_service.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="estimatedMinutesWatched",
            filters=filter_str,
        ).execute()
        rows = wt_resp.get("rows") or []
        if rows:
            snap.watch_time_minutes = float(rows[0][0])
        snap.raw["watch_time"] = wt_resp
    except Exception as e:
        logger.warning(f"[yt-analytics] watch time failed: {e}")

    return snap


# ----------------------------------------------------------------------
def _get_services():
    """Return (data_service, analytics_service) or None if auth is unavailable."""
    try:
        from googleapiclient.discovery import build
        from publisher.youtube_api import YouTubeAPIPublisher
    except Exception as e:
        logger.warning(f"[yt-analytics] google client unavailable: {e}")
        return None
    try:
        pub = YouTubeAPIPublisher()
        # _get_service builds the "youtube" Data API client; we reuse its creds.
        data_service = pub._get_service()
        # Pull the creds object back out — set in pub._get_service.
        creds = getattr(data_service, "_http", None)
        # The googleapiclient builds the service from creds; easier path is to
        # re-build both with the same Credentials object.
        from google.oauth2.credentials import Credentials
        from pathlib import Path
        token_path = Path(pub.token_path)
        if not token_path.exists():
            return None
        creds = Credentials.from_authorized_user_file(str(token_path), scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/yt-analytics.readonly",
        ])
        data_service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        analytics_service = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        return data_service, analytics_service
    except Exception as e:
        logger.warning(f"[yt-analytics] service build failed: {e}")
        return None


def _parse_iso8601_duration(s: str) -> float:
    """Tiny PT#M#S parser sufficient for Shorts. Returns seconds."""
    if not s.startswith("PT"):
        return 0.0
    import re
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0.0
    h = int(m.group(1) or 0)
    mn = int(m.group(2) or 0)
    sec = int(m.group(3) or 0)
    return float(h * 3600 + mn * 60 + sec)
