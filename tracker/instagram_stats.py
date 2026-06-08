"""Fetch engagement metrics for an Instagram Reel."""

from __future__ import annotations


class InstagramStats:
    platform = "instagram"

    def fetch(self, platform_post_id: str) -> dict:
        raise NotImplementedError
