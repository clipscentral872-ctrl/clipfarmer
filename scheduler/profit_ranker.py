"""Profit-ranked campaign picker.

Replaces `pick_next_campaign_for_posting` with EV-based ranking:

    EV(campaign) = expected_views(campaign) * cpm_usd(campaign) / 1000

where:
    expected_views = max(median(competitor top performers),
                         median(our own posted clips for this campaign))
                     * learned_multiplier

Falls back gracefully:
- If no competitor data → use our own median.
- If no own median → use a conservative baseline (10k).
- If no CPM → use 0.50/1k as Clipify's de-facto floor.

Picks among campaigns that still pass the quota + source + budget checks
in `scheduler.quota.pick_next_campaign_for_posting`.
"""

from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from scheduler.quota import (
    DAILY_CLIP_QUOTA,
    daily_clip_count,
    daily_quota_for_campaign,
    _has_source_or_can_find,
)


VIEWS_K_M_B_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)\s*$", re.IGNORECASE)

# Conservative fallback when we have no signal at all.
BASELINE_EXPECTED_VIEWS = 10_000
DEFAULT_CPM_USD = 0.50


def views_to_int(s) -> Optional[int]:
    """Convert '1.2M', '847K', '12,345', 12345, or None → int views."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    m = VIEWS_K_M_B_RE.match(s)
    if not m:
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    n = float(m.group(1))
    suf = m.group(2).upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suf, 1)
    return int(n * mult)


def _competitor_median_views(campaign: dict) -> Optional[int]:
    raw = campaign.get("top_performers")
    if not raw:
        return None
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not items:
        return None
    nums = [views_to_int(it.get("views")) for it in items if isinstance(it, dict)]
    nums = [n for n in nums if n and n > 0]
    if not nums:
        return None
    return int(statistics.median(nums))


def _own_median_views(repo: Repository, campaign_id: int, days: int = 30) -> Optional[int]:
    """Latest views per post for this campaign in the last `days` days,
    then median across posts. Views live in the analytics table; we take
    each post's most recent snapshot."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with repo.conn() as c:
        rows = c.execute(
            "SELECT MAX(a.views) AS v FROM posts p "
            "JOIN clips cl ON cl.id = p.clip_id "
            "JOIN analytics a ON a.post_id = p.id "
            "WHERE cl.campaign_id = ? "
            "AND (p.posted_at IS NULL OR p.posted_at >= ?) "
            "GROUP BY p.id",
            (campaign_id, cutoff),
        ).fetchall()
    nums = [int(r["v"]) for r in rows if r["v"]]
    if not nums:
        return None
    return int(statistics.median(nums))


def expected_views(repo: Repository, campaign: dict) -> int:
    comp = _competitor_median_views(campaign) or 0
    own = _own_median_views(repo, campaign["id"]) or 0
    base = max(comp, own)
    if base <= 0:
        base = BASELINE_EXPECTED_VIEWS
    return base


def cpm_for(campaign: dict) -> float:
    """Pay rate per 1k views in USD. campaigns.payout_per_1k_views is the
    canonical column (set when the campaign was added)."""
    val = campaign.get("payout_per_1k_views")
    try:
        return float(val) if val else DEFAULT_CPM_USD
    except (TypeError, ValueError):
        return DEFAULT_CPM_USD


def score_campaign(repo: Repository, campaign: dict) -> dict:
    """Return {ev_usd, expected_views, cpm, comp_median, own_median, reason}."""
    comp = _competitor_median_views(campaign)
    own = _own_median_views(repo, campaign["id"])
    exp = expected_views(repo, campaign)
    cpm = cpm_for(campaign)
    ev_usd = exp * cpm / 1000.0
    reason_bits = []
    if comp:
        reason_bits.append(f"comp median {_humanize(comp)}")
    if own:
        reason_bits.append(f"own median {_humanize(own)}")
    if not reason_bits:
        reason_bits.append(f"baseline {_humanize(BASELINE_EXPECTED_VIEWS)}")
    reason_bits.append(f"× ${cpm:.2f}/1k = ~${ev_usd:.2f}")
    return {
        "ev_usd": ev_usd,
        "expected_views": exp,
        "cpm": cpm,
        "comp_median": comp,
        "own_median": own,
        "reason": ", ".join(reason_bits),
    }


def pick_next_campaign_by_ev(
    repo: Repository,
    min_budget_pct: Optional[float] = None,
    require_source: bool = True,
) -> Optional[dict]:
    """Pick the highest-EV eligible campaign. Attaches `_pick_reason` to the
    returned dict so the caller can narrate the choice."""
    floor = min_budget_pct if min_budget_pct is not None else settings.min_budget_remaining_pct
    with repo.conn() as c:
        rows = c.execute(
            "SELECT * FROM campaigns "
            "WHERE (status IS NULL OR status='active') "
            "AND (budget_remaining_pct IS NULL OR budget_remaining_pct >= ?)",
            (floor,),
        ).fetchall()

    skip_tiktok = (os.environ.get("SKIP_TIKTOK", "").lower() in ("1", "true", "yes", "on"))

    eligible: list[tuple[float, dict, dict]] = []
    for row in rows:
        camp = dict(row)
        quota = daily_quota_for_campaign(camp)
        if quota <= 0:
            continue
        count = daily_clip_count(repo, camp["id"])
        if count >= quota:
            continue
        if require_source and not _has_source_or_can_find(camp):
            continue
        # If TikTok posting is off, skip campaigns that REQUIRE tiktok (would
        # fail rule validation on the publisher side anyway).
        if skip_tiktok and _campaign_requires_tiktok(camp):
            logger.info(f"[profit-ranker] skip #{camp['id']} {camp['title']} — requires tiktok, SKIP_TIKTOK is on")
            continue
        s = score_campaign(repo, camp)
        eligible.append((s["ev_usd"], camp, s))

    if not eligible:
        logger.info("[profit-ranker] no eligible campaigns")
        return None

    eligible.sort(key=lambda t: t[0], reverse=True)
    ev_usd, camp, s = eligible[0]
    camp["_pick_reason"] = (
        f"#{camp['id']} {camp['title']}: {s['reason']}"
    )
    logger.info(f"[profit-ranker] {camp['_pick_reason']}")
    if len(eligible) > 1:
        runner_ups = ", ".join(
            f"#{c['id']} ${s2['ev_usd']:.2f}" for _, c, s2 in eligible[1:4]
        )
        logger.info(f"[profit-ranker] runners-up: {runner_ups}")
    return camp


def _campaign_requires_tiktok(camp: dict) -> bool:
    """True if structured_rules.platforms_required lists tiktok AND not just YT+IG."""
    raw = camp.get("structured_rules")
    if not raw:
        return False
    try:
        rules = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return False
    required = rules.get("platforms_required") or []
    if not required:
        return False
    return any("tiktok" in str(p).lower() for p in required) and not any(
        "youtube" in str(p).lower() or "instagram" in str(p).lower() for p in required
    )


def _humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)
