"""Brain reflection — self-critique and calibration.

The Brain makes predictions all over the place:
  - Director: "predicted_value_per_clip = $X"
  - EV ranker: "ai_score 84 means this clip will perform"
  - Experiment: "if we try Y, we expect Z"
  - QA: "rejection risk 75%"

Without reflection, the Brain stays naïvely confident even when its
predictions are wrong. This module runs weekly:

  1. Pulls every prediction the Brain made vs the actual outcome.
  2. Computes calibration: bias (consistent over/under), and accuracy.
  3. If a predictor is biased, applies a correction multiplier to future
     predictions of that type (Director EV, etc).
  4. Telegrams a calibration report so Chris sees how well the Brain
     actually knows what it's talking about.

This is what makes the Brain genuinely "learning about itself."
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from scheduler.profit_ranker import cpm_for


# Apply correction multipliers only if we have at least this many data points.
MIN_OBSERVATIONS_FOR_CALIBRATION = 5


def reflect(repo: Repository) -> dict:
    """Run all calibration checks + persist correction factors. Returns
    a dict suitable for Telegram + later inspection."""
    _ensure_correction_table(repo)
    report: dict = {
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report["director_ev"] = _calibrate_director_ev(repo)
    report["ai_score"] = _calibrate_ai_score(repo)
    report["experiments"] = _summarize_experiment_record(repo)
    return report


def get_correction(repo: Repository, key: str) -> float:
    """Look up the correction multiplier for a predictor type. 1.0 = no
    adjustment. Used by Director / ranker to scale future predictions."""
    _ensure_correction_table(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT multiplier FROM brain_corrections WHERE key=?", (key,)
        ).fetchone()
    return float(row["multiplier"]) if row else 1.0


def _set_correction(repo: Repository, key: str, multiplier: float, note: str = "") -> None:
    with repo.conn() as c:
        c.execute(
            "INSERT INTO brain_corrections (key, multiplier, note, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET multiplier=excluded.multiplier, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (key, multiplier, note,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _calibrate_director_ev(repo: Repository) -> dict:
    """Compare each campaign's Director.predicted_value_per_clip vs the
    median ACTUAL implied earnings per clip (views × CPM / 1000).
    Returns {bias_ratio, n, multiplier_applied}."""
    pairs: list[tuple[float, float]] = []
    with repo.conn() as c:
        rows = c.execute(
            "SELECT id, creative_brief, payout_per_1k_views FROM campaigns "
            "WHERE creative_brief IS NOT NULL AND (status IS NULL OR status='active')"
        ).fetchall()
        for r in rows:
            try:
                brief = json.loads(r["creative_brief"])
            except Exception:
                continue
            pred = brief.get("predicted_value_per_clip")
            if not pred or float(pred) <= 0:
                continue
            cpm = float(r["payout_per_1k_views"] or 0.50)
            # Actual: median views * cpm / 1000 across this campaign's posted clips
            actual_views = c.execute(
                "SELECT MAX(a.views) AS v FROM posts p "
                "JOIN clips cl ON cl.id = p.clip_id "
                "JOIN analytics a ON a.post_id = p.id "
                "WHERE cl.campaign_id = ? AND p.status='posted' GROUP BY p.id",
                (r["id"],),
            ).fetchall()
            vs = [int(x["v"]) for x in actual_views if x["v"]]
            if not vs:
                continue
            actual_ev = statistics.median(vs) * cpm / 1000.0
            pairs.append((float(pred), actual_ev))

    if len(pairs) < MIN_OBSERVATIONS_FOR_CALIBRATION:
        return {"n": len(pairs), "bias_ratio": None,
                "note": "not enough data for calibration yet"}

    # Bias = mean(actual / predicted). >1 = Director under-predicting, <1 = over.
    ratios = [actual / pred for pred, actual in pairs if pred > 0]
    bias = statistics.mean(ratios)
    # Clamp the correction to a sane band so a few outliers don't blow it up.
    mult = max(0.25, min(4.0, bias))
    _set_correction(repo, "director_ev", mult,
                    note=f"based on {len(pairs)} campaigns, bias={bias:.2f}")
    logger.info(f"[reflect] director_ev calibration: bias={bias:.2f}, mult={mult:.2f}")
    return {"n": len(pairs), "bias_ratio": round(bias, 3),
            "multiplier_applied": round(mult, 3)}


def _calibrate_ai_score(repo: Repository) -> dict:
    """Does ai_score actually correlate with views? Computes Spearman-style
    rank correlation between (ai_score, total_views) across all posted clips.
    Reports 'reliability' so the EV ranker knows how much to trust it."""
    pairs: list[tuple[float, int]] = []
    with repo.conn() as c:
        rows = c.execute(
            "SELECT cl.ai_score, cl.id FROM clips cl "
            "JOIN posts p ON p.clip_id = cl.id "
            "WHERE p.status='posted' AND cl.ai_score IS NOT NULL "
            "GROUP BY cl.id"
        ).fetchall()
        for r in rows:
            v = c.execute(
                "SELECT MAX(a.views) AS v FROM posts p "
                "JOIN analytics a ON a.post_id = p.id "
                "WHERE p.clip_id = ? GROUP BY p.id",
                (r["id"],),
            ).fetchall()
            vs = [int(x["v"]) for x in v if x["v"]]
            if vs:
                pairs.append((float(r["ai_score"]), max(vs)))
    if len(pairs) < MIN_OBSERVATIONS_FOR_CALIBRATION:
        return {"n": len(pairs), "correlation": None,
                "note": "not enough data for ai_score calibration yet"}
    # Spearman: rank-correlate
    by_score = sorted(pairs, key=lambda p: p[0])
    by_views = sorted(pairs, key=lambda p: p[1])
    score_rank = {id(p): i for i, p in enumerate(by_score)}
    views_rank = {id(p): i for i, p in enumerate(by_views)}
    n = len(pairs)
    sum_d_sq = sum((score_rank[id(p)] - views_rank[id(p)]) ** 2 for p in pairs)
    rho = 1 - (6 * sum_d_sq) / (n * (n * n - 1)) if n > 1 else 0
    _set_correction(repo, "ai_score_reliability",
                    max(0.0, min(1.0, (rho + 1) / 2)),
                    note=f"Spearman ρ={rho:.2f} over {n} clips")
    logger.info(f"[reflect] ai_score correlation ρ={rho:.2f} over {n} clips")
    return {"n": n, "correlation": round(rho, 3)}


def _summarize_experiment_record(repo: Repository) -> dict:
    """Of all experiments that have outcomes, how often were they hits?"""
    with repo.conn() as c:
        rows = c.execute(
            "SELECT experiment_outcome FROM clips "
            "WHERE experiment_hypothesis IS NOT NULL "
            "AND experiment_outcome IS NOT NULL"
        ).fetchall()
    if not rows:
        return {"n": 0, "hit_rate": None}
    verdicts: list[str] = []
    for r in rows:
        try:
            v = json.loads(r["experiment_outcome"])
            verdicts.append(v.get("verdict", "neutral"))
        except Exception:
            continue
    n = len(verdicts)
    hits = sum(1 for v in verdicts if v == "hit")
    return {"n": n, "hit_rate": round(hits / n, 3) if n else None}


def render_telegram(report: dict) -> str:
    lines = ["<b>🧠 Brain self-reflection (weekly)</b>"]
    de = report.get("director_ev") or {}
    if de.get("bias_ratio") is not None:
        bias = de["bias_ratio"]
        verdict = ("over-predicting (cuts targets)" if bias < 0.7
                   else "under-predicting (misses upside)" if bias > 1.4
                   else "well-calibrated")
        lines.append(
            f"\n<b>Director's predicted $/clip:</b>\n"
            f"  Actual / predicted = {bias:.2f}× ({verdict}, n={de['n']})\n"
            f"  Correction multiplier set to {de.get('multiplier_applied', 1):.2f}×"
        )
    else:
        lines.append(f"\n<b>Director's predicted $/clip:</b> {de.get('note', 'n/a')}")
    sc = report.get("ai_score") or {}
    if sc.get("correlation") is not None:
        rho = sc["correlation"]
        verdict = ("strong predictor" if rho >= 0.5
                   else "weak predictor" if rho >= 0.2
                   else "not predictive — needs more data or model swap")
        lines.append(
            f"\n<b>AI score vs views correlation:</b>\n"
            f"  ρ = {rho:.2f} ({verdict}, n={sc['n']})"
        )
    ex = report.get("experiments") or {}
    if ex.get("hit_rate") is not None:
        lines.append(
            f"\n<b>Experiments:</b> {int(ex['hit_rate'] * 100)}% hit rate "
            f"over {ex['n']} tested"
        )
    return "\n".join(lines)


def notify(report: dict) -> None:
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if gate.enabled:
            gate.notify(render_telegram(report))
    except Exception as e:
        logger.warning(f"[reflect] notify failed: {e}")


_CORRECTIONS_CHECKED = False


def _ensure_correction_table(repo: Repository) -> None:
    global _CORRECTIONS_CHECKED
    if _CORRECTIONS_CHECKED:
        return
    with repo.conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS brain_corrections ("
            "key TEXT PRIMARY KEY, multiplier REAL NOT NULL, "
            "note TEXT, updated_at TEXT NOT NULL)"
        )
    _CORRECTIONS_CHECKED = True
