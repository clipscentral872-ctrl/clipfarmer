"""Attribute outcomes back to specific Brain experiments.

For every posted clip that carries an `experiment_hypothesis`, we sum
total views/likes across its platforms and emit a verdict per
experiment:

  - hit     : exceeded campaign baseline median by `WIN_LIFT`
  - miss    : fell below campaign baseline median by `LOSS_LIFT`
  - neutral : within band

The verdicts are Telegram-narrated so Chris knows whether the bets the
Brain placed actually paid out. Verdicts also persist on the clip row
(`experiment_outcome`) so future brain runs can use the data.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from .analyst import build_outcome_records
from .learnings import get_learnings


WIN_LIFT = 1.2
LOSS_LIFT = 0.8


def refresh_outcomes(repo: Repository) -> list[dict]:
    """Walk every clip with an experiment hypothesis; emit verdicts."""
    _ensure_column(repo)
    with repo.conn() as c:
        rows = c.execute(
            "SELECT cl.id, cl.campaign_id, cl.experiment_hypothesis, "
            "cl.experiment_params, cl.experiment_outcome "
            "FROM clips cl JOIN posts p ON p.clip_id = cl.id "
            "WHERE cl.experiment_hypothesis IS NOT NULL AND p.status='posted' "
            "GROUP BY cl.id"
        ).fetchall()
    if not rows:
        return []

    verdicts: list[dict] = []
    for row in rows:
        if row["experiment_outcome"]:
            # Already attributed; skip.
            continue
        verdict = _judge(repo, row["id"], row["campaign_id"])
        if not verdict:
            continue
        with repo.conn() as c:
            c.execute(
                "UPDATE clips SET experiment_outcome = ? WHERE id = ?",
                (json.dumps(verdict), row["id"]),
            )
        verdicts.append({
            "clip_id": row["id"],
            "campaign_id": row["campaign_id"],
            "hypothesis": row["experiment_hypothesis"],
            "verdict": verdict["verdict"],
            "lift": verdict["lift"],
            "views": verdict["views"],
        })
    return verdicts


def refresh_and_notify(repo: Repository) -> list[dict]:
    out = refresh_outcomes(repo)
    if not out:
        return out
    msgs: list[str] = []
    for v in out:
        icon = {"hit": "🎯", "miss": "❌", "neutral": "➖"}.get(v["verdict"], "•")
        msgs.append(
            f"{icon} <b>Experiment {v['verdict']}</b> on #{v['campaign_id']}\n"
            f"   <i>{v['hypothesis'][:140]}</i>\n"
            f"   {v['views']:,} views — {v['lift']:.2f}× baseline"
        )
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if gate.enabled:
            gate.notify("<b>📊 Experiment outcomes update</b>\n\n" + "\n\n".join(msgs))
    except Exception as e:
        logger.warning(f"[exp-outcomes] telegram failed: {e}")
    return out


def _judge(repo: Repository, clip_id: int, campaign_id: int) -> Optional[dict]:
    """Return verdict for one clip, or None if there's no analytics yet."""
    with repo.conn() as c:
        rows = c.execute(
            "SELECT MAX(a.views) AS v FROM posts p "
            "JOIN analytics a ON a.post_id = p.id "
            "WHERE p.clip_id = ? GROUP BY p.id",
            (clip_id,),
        ).fetchall()
    nums = [int(r["v"]) for r in rows if r["v"]]
    if not nums:
        return None
    total_views = sum(nums)

    learnings = get_learnings(repo, campaign_id)
    baseline = (learnings or {}).get("baseline_median_views") or 0
    if baseline <= 0:
        # Fall back to median across this campaign's existing outcomes.
        outcomes = build_outcome_records(repo, campaign_id=campaign_id)
        nums_others = [o.total_views for o in outcomes if o.total_views > 0]
        baseline = int(statistics.median(nums_others)) if nums_others else total_views

    if baseline <= 0:
        return None
    lift = total_views / baseline
    if lift >= WIN_LIFT:
        v = "hit"
    elif lift <= LOSS_LIFT:
        v = "miss"
    else:
        v = "neutral"
    return {
        "verdict": v,
        "views": total_views,
        "baseline": baseline,
        "lift": round(lift, 2),
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


_COL_CHECKED = False


def _ensure_column(repo: Repository) -> None:
    global _COL_CHECKED
    if _COL_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(clips)").fetchall()}
        if "experiment_outcome" not in cols:
            c.execute("ALTER TABLE clips ADD COLUMN experiment_outcome TEXT")
    _COL_CHECKED = True
