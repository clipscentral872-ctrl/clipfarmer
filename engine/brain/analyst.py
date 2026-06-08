"""Build per-clip outcome records by joining clips ↔ posts ↔ analytics.

The output is a `ClipOutcome` dataclass per posted clip, carrying both
the clip's pre-post features (what the engine knew when it cut it) and
its post-post outcomes (views / engagement / earnings) per platform.

Downstream: `learnings.py` aggregates these into per-campaign patterns.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from db.repository import Repository


# Duration buckets used as a categorical feature.
def _duration_bucket(sec: float) -> str:
    if sec < 35: return "u35"
    if sec < 45: return "35-45"
    if sec < 55: return "45-55"
    return "55+"


def _score_bucket(score: Optional[float]) -> str:
    if score is None: return "unknown"
    if score >= 90: return "90+"
    if score >= 80: return "80-90"
    if score >= 70: return "70-80"
    return "u70"


def _hashtag_bucket(n: int) -> str:
    if n <= 2: return "u3"
    if n <= 4: return "3-4"
    return "5+"


def _time_bucket(posted_at_iso: Optional[str]) -> str:
    """Bucket posted_at UTC hour into broad US-targeting windows.
    (Slots are scheduled in America/New_York; this groups them by daypart.)"""
    if not posted_at_iso:
        return "unknown"
    try:
        # Treat all stored ISO timestamps as UTC.
        from datetime import datetime
        dt = datetime.fromisoformat(posted_at_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "unknown"
    # Convert to US Eastern hour for bucketing.
    try:
        from zoneinfo import ZoneInfo
        h = dt.astimezone(ZoneInfo("America/New_York")).hour
    except Exception:
        h = dt.hour
    if 5 <= h < 11: return "et-morning"        # 5am-11am ET
    if 11 <= h < 14: return "et-midday"        # 11am-2pm ET
    if 14 <= h < 18: return "et-afternoon"     # 2pm-6pm ET
    if 18 <= h < 22: return "et-evening"       # 6pm-10pm ET
    return "et-night"                          # 10pm-5am ET


def _hook_style(hook: str) -> str:
    h = (hook or "").strip()
    if not h:
        return "none"
    if h.endswith("?"):
        return "question"
    if h.lower().startswith(("how ", "why ", "what ", "when ", "who ")):
        return "wh-open"
    if h.endswith("!"):
        return "exclaim"
    return "statement"


@dataclass
class ClipOutcome:
    clip_id: int
    campaign_id: int
    duration_sec: float
    ai_score: Optional[float]
    hook_text: Optional[str]
    caption_text: Optional[str]
    n_hashtags: int
    # Per-platform views (latest snapshot per post)
    views_by_platform: dict[str, int] = field(default_factory=dict)
    likes_by_platform: dict[str, int] = field(default_factory=dict)

    @property
    def total_views(self) -> int:
        return sum(self.views_by_platform.values())

    content_type: Optional[str] = None  # set by style_classifier; None for legacy clips
    posted_at: Optional[str] = None     # used for time-of-day bucket

    def features(self) -> dict[str, str]:
        """Categorical features used by the learner."""
        f = {
            "duration_bucket": _duration_bucket(self.duration_sec),
            "ai_score_bucket": _score_bucket(self.ai_score),
            "hashtag_bucket": _hashtag_bucket(self.n_hashtags),
            "hook_style": _hook_style(self.hook_text or ""),
            "time_of_day": _time_bucket(self.posted_at),
        }
        if self.content_type:
            f["content_type"] = self.content_type
        return f


def build_outcome_records(
    repo: Repository,
    campaign_id: Optional[int] = None,
    min_age_hours: int = 24,
) -> list[ClipOutcome]:
    """Return one ClipOutcome per posted-and-tracked clip.

    - `campaign_id`: limit to one campaign, else all.
    - `min_age_hours`: skip clips posted in the last N hours (too soon to score).
    """
    with repo.conn() as c:
        # A clip counts as "outcome-ready" once it has at least one post
        # in status='posted'. Clip.status itself is set by the engine
        # ('ready' when produced) and isn't a posting-state marker.
        sql = (
            "SELECT DISTINCT cl.id AS clip_id, cl.campaign_id, cl.duration_sec, "
            "cl.ai_score, cl.hook_text, cl.caption_text, cl.suggested_hashtags, "
            "cl.content_type "
            "FROM clips cl "
            "JOIN posts p ON p.clip_id = cl.id "
            "WHERE p.status = 'posted' "
        )
        params: list = []
        if campaign_id is not None:
            sql += "AND cl.campaign_id = ? "
            params.append(campaign_id)
        clip_rows = c.execute(sql, params).fetchall()

    if not clip_rows:
        return []

    outcomes: list[ClipOutcome] = []
    with repo.conn() as c:
        for row in clip_rows:
            tags_raw = row["suggested_hashtags"]
            try:
                tags = json.loads(tags_raw) if tags_raw else []
            except json.JSONDecodeError:
                tags = []
            outcome = ClipOutcome(
                clip_id=row["clip_id"],
                campaign_id=row["campaign_id"],
                duration_sec=float(row["duration_sec"] or 0),
                ai_score=row["ai_score"],
                hook_text=row["hook_text"],
                caption_text=row["caption_text"],
                n_hashtags=len(tags) if isinstance(tags, list) else 0,
                content_type=row["content_type"],
            )

            # For each post of this clip, take the max view snapshot.
            earliest_posted_at: Optional[str] = None
            for p in c.execute(
                "SELECT p.id, p.platform, p.posted_at FROM posts p "
                "WHERE p.clip_id = ? AND p.status = 'posted'",
                (row["clip_id"],),
            ).fetchall():
                if p["posted_at"] and (earliest_posted_at is None or p["posted_at"] < earliest_posted_at):
                    earliest_posted_at = p["posted_at"]
                a = c.execute(
                    "SELECT MAX(views) AS v, MAX(likes) AS l FROM analytics WHERE post_id = ?",
                    (p["id"],),
                ).fetchone()
                if a and a["v"]:
                    outcome.views_by_platform[p["platform"]] = int(a["v"])
                if a and a["l"]:
                    outcome.likes_by_platform[p["platform"]] = int(a["l"])
            outcome.posted_at = earliest_posted_at
            if outcome.total_views > 0:
                outcomes.append(outcome)

    logger.info(f"[brain.analyst] built {len(outcomes)} outcome record(s)"
                f"{f' for campaign #{campaign_id}' if campaign_id else ''}")
    return outcomes


def median_views(outcomes: list[ClipOutcome]) -> int:
    nums = [o.total_views for o in outcomes if o.total_views > 0]
    if not nums:
        return 0
    return int(statistics.median(nums))
