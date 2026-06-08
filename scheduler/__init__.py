"""Scheduling layer: pacing + per-campaign daily quotas.

Chris runs 3 campaigns × 2 clips/day = 6 clips/day across the system.
This package owns:
  - Daily clip-count per campaign (how many clips we've produced+posted today)
  - A picker that returns the next viable campaign under its daily quota
  - A `run_one_slot` function that runs the orchestrator pipeline for one
    clip on the picked campaign, then exits
  - A long-running loop (`python -m scheduler`) that fires slots at fixed
    times across the day so the YouTube quota + IG rate limits never burst

Everything is idempotent: if a slot fires and no campaign is eligible
(all at quota, or no source video downloaded yet), the slot is a no-op
and we wait for the next one.
"""

from .quota import (
    DAILY_CLIP_QUOTA,
    SLOT_TIMES_LOCAL,
    daily_clip_count,
    pick_next_campaign_for_posting,
)

__all__ = [
    "DAILY_CLIP_QUOTA",
    "SLOT_TIMES_LOCAL",
    "daily_clip_count",
    "pick_next_campaign_for_posting",
]
