"""Cross-campaign pattern transfer.

What works for #44 Jacks (food/travel) doesn't currently inform #51
Dhar Mann (podcast). That's a learning loss — patterns that win across
DIFFERENT campaigns are stronger signal than patterns that win in just
one.

This module finds features that win across 2+ campaigns and adds them
to a "system-wide patterns" blob the Director consumes when briefing a
NEW campaign. Lets the Brain bootstrap a brand-new campaign with what
it's already learned globally.

Example output (system-wide):
  - content_type=person-to-camera wins across 3 campaigns (avg lift 1.6×)
  - hook_style=question wins across 2 campaigns (avg lift 1.4×)
  - youtube platform dominates for 4 of 5 campaigns

When briefing a brand-new campaign with no history, the Director
prompt includes "across our existing 5 campaigns, what tends to win"
as prior — much better than starting from scratch.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from .learnings import get_learnings


# Minimum campaigns where a feature value must appear AS A WINNER to
# count as a cross-campaign pattern. 2 is the floor; bigger is stronger.
MIN_CAMPAIGNS_FOR_GLOBAL_PATTERN = 2


def refresh(repo: Repository) -> dict:
    """Compute cross-campaign winners. Persists into a system-wide blob
    that lives on a single sentinel row in `brain_global` table."""
    _ensure_global_table(repo)
    # Gather per-campaign winners.
    winners_per_campaign: dict[int, list[dict]] = {}
    platform_per_campaign: dict[int, list[dict]] = {}
    with repo.conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM campaigns WHERE (status IS NULL OR status='active')"
        ).fetchall()]
    for cid in ids:
        learn = get_learnings(repo, cid)
        if not learn:
            continue
        winners_per_campaign[cid] = learn.get("winners") or []
        platform_per_campaign[cid] = learn.get("platform_recommendations") or []

    # Aggregate (feature, value) → count + avg lift across campaigns.
    feature_agg: dict[tuple[str, str], list[float]] = {}
    for cid, winners in winners_per_campaign.items():
        seen = set()
        for w in winners:
            key = (w.get("feature"), w.get("value"))
            if key in seen:
                continue
            seen.add(key)
            feature_agg.setdefault(key, []).append(float(w.get("lift", 1.0)))

    global_patterns = []
    for (feat, val), lifts in feature_agg.items():
        if len(lifts) >= MIN_CAMPAIGNS_FOR_GLOBAL_PATTERN:
            global_patterns.append({
                "feature": feat, "value": val,
                "n_campaigns": len(lifts),
                "avg_lift": round(statistics.mean(lifts), 3),
            })
    global_patterns.sort(key=lambda p: (p["n_campaigns"], p["avg_lift"]), reverse=True)

    # Platform dominance: which platform is "prioritize" most often?
    plat_votes: dict[str, dict[str, int]] = {}
    for cid, recs in platform_per_campaign.items():
        for r in recs:
            p = (r.get("platform") or "").lower()
            a = (r.get("action") or "").lower()
            if not p or not a:
                continue
            plat_votes.setdefault(p, {"prioritize": 0, "drop": 0, "fine": 0})
            if a in plat_votes[p]:
                plat_votes[p][a] += 1

    platform_summary = []
    for p, counts in plat_votes.items():
        total = sum(counts.values())
        if total < MIN_CAMPAIGNS_FOR_GLOBAL_PATTERN:
            continue
        platform_summary.append({
            "platform": p,
            "prioritize_share": round(counts["prioritize"] / total, 2),
            "drop_share": round(counts["drop"] / total, 2),
            "n": total,
        })
    platform_summary.sort(key=lambda x: x["prioritize_share"], reverse=True)

    payload = {
        "global_patterns": global_patterns,
        "platform_summary": platform_summary,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_campaigns_analyzed": len(winners_per_campaign),
    }
    _persist(repo, payload)
    logger.info(
        f"[cross-pattern] {len(global_patterns)} global pattern(s) across "
        f"{len(winners_per_campaign)} campaigns"
    )
    return payload


def get_global_patterns(repo: Repository) -> Optional[dict]:
    _ensure_global_table(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT payload FROM brain_global WHERE key='cross_patterns'"
        ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def render_for_director(repo: Repository) -> str:
    """Compact prompt-injectable summary for the Director's prompt."""
    pat = get_global_patterns(repo)
    if not pat:
        return ""
    lines = ["What's been working ACROSS our existing campaigns (treat as priors):"]
    for p in pat.get("global_patterns", [])[:5]:
        lines.append(
            f"- {p['feature']}={p['value']} wins in {p['n_campaigns']} campaigns "
            f"(avg {p['avg_lift']}× lift)"
        )
    for ps in pat.get("platform_summary", [])[:3]:
        if ps["prioritize_share"] >= 0.5:
            lines.append(
                f"- {ps['platform']}: prioritize in {int(ps['prioritize_share'] * 100)}% "
                f"of campaigns we've tested"
            )
        elif ps["drop_share"] >= 0.5:
            lines.append(
                f"- {ps['platform']}: underperforms in {int(ps['drop_share'] * 100)}% of campaigns"
            )
    return "\n".join(lines) if len(lines) > 1 else ""


_GLOBAL_TABLE_CHECKED = False


def _ensure_global_table(repo: Repository) -> None:
    global _GLOBAL_TABLE_CHECKED
    if _GLOBAL_TABLE_CHECKED:
        return
    with repo.conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS brain_global ("
            "key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    _GLOBAL_TABLE_CHECKED = True


def _persist(repo: Repository, payload: dict) -> None:
    with repo.conn() as c:
        c.execute(
            "INSERT INTO brain_global (key, payload, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            ("cross_patterns", json.dumps(payload),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
