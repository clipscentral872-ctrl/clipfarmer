"""Aggregate ClipOutcome records into per-campaign learnings.

For each (feature_name, feature_value), compute the median total-views of
clips that had that feature. Then identify the WINNING values per feature
— those whose median is materially above the campaign baseline.

Output shape per campaign:
    {
      "baseline_median_views": int,
      "n_clips": int,
      "by_feature": {
          "hook_style": {
              "question": {"median": 47000, "n": 3, "lift": 1.5},
              "statement": {"median": 31000, "n": 4, "lift": 1.0},
              ...
          },
          "duration_bucket": {...},
          ...
      },
      "winners": [
          {"feature": "hook_style", "value": "question", "median": 47000, "lift": 1.5},
          ...
      ],
      "computed_at": "ISO-8601",
    }

Persisted as JSON on the campaign row (we reuse the `top_performers`
adjacent style — add a `learnings` column).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from .analyst import ClipOutcome, build_outcome_records, median_views


# Minimum clips with the SAME feature value before we trust the median.
MIN_CLIPS_PER_FEATURE_VALUE = 2
# Treat a feature value as a "winner" if its median is this much above baseline.
WINNER_LIFT_THRESHOLD = 1.25
# Minimum clips total per campaign before we report any learnings.
MIN_CLIPS_PER_CAMPAIGN = 3


def refresh_learnings(
    repo: Repository,
    campaign_id: Optional[int] = None,
) -> dict[int, dict]:
    """Recompute learnings for one campaign (or every campaign with outcomes)
    and persist them. Returns {campaign_id: learnings_dict}."""
    _ensure_learnings_column(repo)

    if campaign_id is not None:
        campaign_ids = [campaign_id]
    else:
        with repo.conn() as c:
            campaign_ids = [r["campaign_id"] for r in c.execute(
                "SELECT DISTINCT cl.campaign_id FROM clips cl "
                "JOIN posts p ON p.clip_id = cl.id "
                "WHERE p.status='posted'"
            ).fetchall()]

    result: dict[int, dict] = {}
    for cid in campaign_ids:
        outcomes = build_outcome_records(repo, campaign_id=cid)
        if len(outcomes) < MIN_CLIPS_PER_CAMPAIGN:
            logger.info(
                f"[brain.learnings] campaign #{cid}: only {len(outcomes)} clips, "
                f"need {MIN_CLIPS_PER_CAMPAIGN}+ — skipping"
            )
            continue
        learnings = _aggregate(outcomes)
        # Diff against the previously persisted learnings BEFORE saving,
        # so the discoveries notifier compares old → new.
        try:
            from .discoveries import detect_and_notify
            notifications = detect_and_notify(repo, cid, learnings)
            if notifications:
                logger.info(f"[brain.learnings] sent {len(notifications)} discovery notification(s) for #{cid}")
        except Exception as e:
            logger.warning(f"[brain.learnings] discoveries failed for #{cid}: {e}")
        _persist(repo, cid, learnings)
        result[cid] = learnings
        winners = ", ".join(
            f"{w['feature']}={w['value']}({w['lift']:.2f}×)"
            for w in learnings["winners"][:4]
        ) or "(no clear winners yet)"
        logger.info(
            f"[brain.learnings] campaign #{cid}: {learnings['n_clips']} clips, "
            f"baseline median {learnings['baseline_median_views']:,} views. Winners: {winners}"
        )
    return result


def get_learnings(repo: Repository, campaign_id: int) -> Optional[dict]:
    """Return the persisted learnings JSON for a campaign, or None."""
    _ensure_learnings_column(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT learnings FROM campaigns WHERE id = ?", (campaign_id,),
        ).fetchone()
    if not row or not row["learnings"]:
        return None
    try:
        return json.loads(row["learnings"])
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------
def _aggregate(outcomes: list[ClipOutcome]) -> dict:
    baseline = median_views(outcomes)
    by_platform = _aggregate_by_platform(outcomes)
    if baseline <= 0:
        return {
            "baseline_median_views": 0,
            "n_clips": len(outcomes),
            "by_feature": {},
            "winners": [],
            "by_platform": by_platform,
            "platform_recommendations": _platform_recs(by_platform),
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # Group clips by every (feature, value) pair they had.
    grouped: dict[str, dict[str, list[int]]] = {}
    for o in outcomes:
        for feat, val in o.features().items():
            grouped.setdefault(feat, {}).setdefault(val, []).append(o.total_views)

    by_feature: dict[str, dict[str, dict]] = {}
    winners: list[dict] = []
    for feat, val_map in grouped.items():
        by_feature[feat] = {}
        for val, view_list in val_map.items():
            if len(view_list) < MIN_CLIPS_PER_FEATURE_VALUE:
                continue
            med = int(statistics.median(view_list))
            lift = round(med / baseline, 3) if baseline else 0.0
            by_feature[feat][val] = {
                "median": med,
                "n": len(view_list),
                "lift": lift,
            }
            if lift >= WINNER_LIFT_THRESHOLD:
                winners.append({
                    "feature": feat,
                    "value": val,
                    "median": med,
                    "lift": lift,
                    "n": len(view_list),
                })
    winners.sort(key=lambda w: w["lift"], reverse=True)

    return {
        "baseline_median_views": baseline,
        "n_clips": len(outcomes),
        "by_feature": by_feature,
        "winners": winners,
        "by_platform": by_platform,
        "platform_recommendations": _platform_recs(by_platform),
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# Min posts per platform before we trust its median + recommendation.
MIN_POSTS_PER_PLATFORM = 2
# Below this ratio vs the best platform → recommend dropping the platform.
PLATFORM_DROP_RATIO = 0.25
# Above this lift vs avg → recommend doubling down.
PLATFORM_PRIORITIZE_RATIO = 1.5


def _aggregate_by_platform(outcomes: list[ClipOutcome]) -> dict[str, dict]:
    """Per-platform median views across all this campaign's posted clips."""
    buckets: dict[str, list[int]] = {}
    for o in outcomes:
        for plat, v in o.views_by_platform.items():
            if v > 0:
                buckets.setdefault(plat, []).append(v)
    out: dict[str, dict] = {}
    for plat, vs in buckets.items():
        out[plat] = {
            "n": len(vs),
            "median_views": int(statistics.median(vs)),
            "total_views": sum(vs),
        }
    return out


def _platform_recs(by_platform: dict[str, dict]) -> list[dict]:
    """Per-platform recommendation: prioritize / fine / drop."""
    eligible = {p: d for p, d in by_platform.items() if d["n"] >= MIN_POSTS_PER_PLATFORM}
    if not eligible:
        return []
    best_median = max(d["median_views"] for d in eligible.values())
    avg_median = statistics.mean(d["median_views"] for d in eligible.values())
    recs = []
    for plat, d in eligible.items():
        med = d["median_views"]
        ratio_to_best = med / best_median if best_median else 0.0
        ratio_to_avg = med / avg_median if avg_median else 0.0
        if ratio_to_best <= PLATFORM_DROP_RATIO:
            action = "drop"
        elif ratio_to_avg >= PLATFORM_PRIORITIZE_RATIO:
            action = "prioritize"
        else:
            action = "fine"
        recs.append({
            "platform": plat,
            "action": action,
            "median_views": med,
            "n": d["n"],
            "ratio_to_best": round(ratio_to_best, 3),
        })
    recs.sort(key=lambda r: r["median_views"], reverse=True)
    return recs


# ----------------------------------------------------------------------
_LEARNINGS_COLUMN_CHECKED = False


def _ensure_learnings_column(repo: Repository) -> None:
    """Add campaigns.learnings TEXT column if it isn't there yet."""
    global _LEARNINGS_COLUMN_CHECKED
    if _LEARNINGS_COLUMN_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "learnings" not in cols:
            logger.info("[brain.learnings] adding campaigns.learnings column")
            c.execute("ALTER TABLE campaigns ADD COLUMN learnings TEXT")
    _LEARNINGS_COLUMN_CHECKED = True


def _persist(repo: Repository, campaign_id: int, learnings: dict) -> None:
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET learnings = ? WHERE id = ?",
            (json.dumps(learnings), campaign_id),
        )
