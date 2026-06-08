"""Fetch engagement metrics for a TikTok post."""

from __future__ import annotations


class TikTokStats:
    platform = "tiktok"

    def fetch(self, platform_post_id: str) -> dict:
        """Return dict with keys: views, likes, comments, shares, saves, raw."""
        raise NotImplementedError
