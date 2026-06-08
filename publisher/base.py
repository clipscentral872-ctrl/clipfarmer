"""Shared types for the publisher module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PublishResult:
    """One row per platform we asked Metricool to post to."""

    platform: str                       # tiktok | youtube | instagram
    platform_post_id: str               # platform-native id (when Metricool returns it)
    post_url: str                       # public URL of the post (when Metricool returns it)
    metricool_post_id: str              # Metricool's own internal id
    scheduled_for: str                  # ISO timestamp, may equal posted_at if immediate
    raw: dict = field(default_factory=dict)
