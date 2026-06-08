"""Fetch engagement metrics for a YouTube Short."""

from __future__ import annotations


class YouTubeStats:
    platform = "youtube"

    def fetch(self, platform_post_id: str) -> dict:
        raise NotImplementedError
