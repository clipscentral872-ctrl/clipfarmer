"""Per-campaign daily quota + picker.

A "clip" is one moment selected by the engine; that clip cross-posts to
3 platforms (TikTok / IG / YouTube), so each clip turns into ~3 posts.
We count CLIPS per campaign, not platform posts — Chris's target of
"2 videos per day per campaign" is 2 clips, not 6 platform posts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository


import json as _json

# Baseline quota; per-campaign quotas FLEX via the latest proposal:
#   promote → baseline + 1
#   keep    → baseline
#   demote  → max(1, baseline - 1)
#   pause   → 0
DAILY_CLIP_QUOTA = 2


def daily_quota_for_campaign(camp: dict) -> int:
    """Return today's allowed clip count for this campaign, honoring the
    latest Brain proposal if present."""
    raw = camp.get("proposal")
    if not raw:
        return DAILY_CLIP_QUOTA
    try:
        prop = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return DAILY_CLIP_QUOTA
    action = (prop.get("action") or "").lower()
    if action == "promote":
        return DAILY_CLIP_QUOTA + 1
    if action == "demote":
        return max(1, DAILY_CLIP_QUOTA - 1)
    if action == "pause":
        return 0
    return DAILY_CLIP_QUOTA

# Posting slots target US viewer peak hours. Chris's local timezone
# (Cape Town, UTC+2) is 7h ahead of US Eastern. US ET prime windows:
#   - Morning lull check (10:00 ET / 17:00 SAST)
#   - Lunch break        (12:00 ET / 19:00 SAST)
#   - After-work first wave (17:00 ET / 00:00 SAST next day)
#   - Evening peak       (20:00 ET / 03:00 SAST next day)
#   - Late-night scroll  (22:00 ET / 05:00 SAST next day)
# `scheduler/__main__` interprets these strings as the scheduler's TZ
# (set to America/New_York there), so they're US Eastern.
SLOT_TIMES_US_EASTERN = ["10:00", "12:00", "15:00", "17:00", "20:00", "22:00"]
# Back-compat alias — older scripts import SLOT_TIMES_LOCAL.
SLOT_TIMES_LOCAL = SLOT_TIMES_US_EASTERN


def daily_clip_count(repo: Repository, campaign_id: int, hours: int = 24) -> int:
    """How many distinct clips for this campaign have been posted in the
    last `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with repo.conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT p.clip_id) FROM posts p "
            "JOIN clips cl ON cl.id = p.clip_id "
            "WHERE cl.campaign_id = ? "
            "AND p.status='posted' "
            "AND p.posted_at IS NOT NULL "
            "AND p.posted_at >= ?",
            (campaign_id, cutoff),
        ).fetchone()
    return int(row[0]) if row else 0


def pick_next_campaign_for_posting(
    repo: Repository,
    min_budget_pct: Optional[float] = None,
    require_source: bool = True,
) -> Optional[dict]:
    """Highest-viability active campaign that:
      - has budget headroom (>= min_budget_pct, default from settings)
      - is below its daily clip quota
      - (optionally) has a downloaded source file ready to clip from

    Returns the campaign row as a dict, or None if nothing's eligible.
    """
    floor = min_budget_pct if min_budget_pct is not None else settings.min_budget_remaining_pct
    with repo.conn() as c:
        rows = c.execute(
            "SELECT * FROM campaigns "
            "WHERE (status IS NULL OR status='active') "
            "AND (budget_remaining_pct IS NULL OR budget_remaining_pct >= ?) "
            "ORDER BY viability_score DESC NULLS LAST",
            (floor,),
        ).fetchall()

    for row in rows:
        camp = dict(row)
        quota = daily_quota_for_campaign(camp)
        count = daily_clip_count(repo, camp["id"])
        if count >= quota:
            logger.debug(
                f"[quota] #{camp['id']} {camp['title']} at quota ({count}/{quota})"
            )
            continue
        if require_source and not _has_source_or_can_find(camp):
            logger.debug(
                f"[quota] #{camp['id']} {camp['title']} skipped — no source "
                f"and source-restricted (can't auto-find)"
            )
            continue
        logger.info(
            f"[quota] pick #{camp['id']} {camp['title']} "
            f"({count}/{quota} today, score={camp.get('viability_score')})"
        )
        return camp
    return None


def _has_source_or_can_find(camp: dict) -> bool:
    """A campaign is workable if it either:
      - already has a downloaded source, OR
      - allows open sourcing (empty source_must_match) so we can auto-find
    """
    if (camp.get("current_source_path") or "").strip():
        return True
    import json as _json
    raw = camp.get("structured_rules")
    if not raw:
        return True  # no rules known yet — assume open until we learn otherwise
    try:
        rules = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return True
    return not (rules.get("source_must_match") or [])
