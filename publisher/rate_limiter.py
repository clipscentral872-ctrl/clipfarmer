"""Per-platform daily post limits + randomized jitter.

Goal: never look robotic to the platforms. Each platform has a max
posts-per-rolling-24h limit and we insert ±20-minute jitter between
scheduled posts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import Repository


DEFAULT_LIMITS = {
    "tiktok": 3,        # conservative — TikTok flags new posters easily
    "youtube": 5,       # YouTube Shorts is more permissive
    "instagram": 4,     # IG flags new posters around 5+/day
}


@dataclass
class RateCheck:
    ok: bool
    reason: str = ""
    posts_in_last_24h: int = 0
    daily_limit: int = 0


def can_post(repo: Repository, platform: str, daily_limit: Optional[int] = None) -> RateCheck:
    limit = daily_limit if daily_limit is not None else DEFAULT_LIMITS.get(platform.lower(), 3)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with repo.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM posts "
            "WHERE platform = ? AND status = 'posted' AND posted_at >= ?",
            (platform.lower(), cutoff),
        ).fetchone()
    posted = row["n"]
    if posted >= limit:
        return RateCheck(
            ok=False,
            reason=f"{platform} already posted {posted} times in last 24h (limit {limit})",
            posts_in_last_24h=posted,
            daily_limit=limit,
        )
    return RateCheck(ok=True, posts_in_last_24h=posted, daily_limit=limit)


def jittered_delay_minutes(base_minutes: int, jitter_pct: float = 0.3) -> int:
    """Return a jittered delay around base_minutes (±jitter_pct%)."""
    j = base_minutes * jitter_pct
    return max(1, int(base_minutes + random.uniform(-j, j)))
