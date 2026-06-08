"""Exploit/Explore allocator for a per-campaign daily clip batch.

Chris's daily target is 2 clips/campaign (DAILY_CLIP_QUOTA). The pattern
we want:
  - Clip #1: exploit  — match the winning content style
  - Clip #2: explore  — try a DIFFERENT style (the next-big-winner test)

If quota is bumped to 3+, the rule generalises to "ceil(2/3) exploit,
remainder explore."

Called by the orchestrator just before scoring; emits a string injected
into the scorer prompt so Claude picks moments matching the bucket.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from db.repository import Repository
from scheduler.quota import DAILY_CLIP_QUOTA, daily_clip_count
from .learnings import get_learnings


# Don't exploit unless this many clips of the winning style have been observed.
MIN_OBSERVATIONS_TO_EXPLOIT = 2
# Lift threshold to call a style "winning" — same as the per-feature winner threshold.
MIN_LIFT_TO_EXPLOIT = 1.25


def slot_intent(repo: Repository, campaign_id: int) -> dict:
    """Decide whether the NEXT clip for this campaign should exploit
    (winning style) or explore (something new). Returns:
        {"mode": "exploit" | "explore",
         "target_style": "<content_type>" | None,
         "avoid_styles": [<content_type>, ...],
         "reason": "<one short sentence>"}
    """
    posted_today = daily_clip_count(repo, campaign_id)
    # Default: explore (no data yet, can't exploit safely).
    intent = {
        "mode": "explore",
        "target_style": None,
        "avoid_styles": [],
        "reason": f"Slot #{posted_today + 1} of {DAILY_CLIP_QUOTA} today",
    }
    learnings = get_learnings(repo, campaign_id)
    if not learnings:
        intent["reason"] = "No learnings yet → explore"
        return intent

    # Find the best content_type winner.
    best_style = _best_content_style(learnings)
    if not best_style:
        intent["reason"] = "No content_type has crossed the lift threshold → explore"
        return intent

    # Plan: of DAILY_CLIP_QUOTA, the LAST one is explore. Everything else exploits.
    is_last_slot = (posted_today + 1) >= DAILY_CLIP_QUOTA
    if is_last_slot:
        intent["mode"] = "explore"
        intent["avoid_styles"] = [best_style["value"]]
        intent["reason"] = (
            f"Slot #{posted_today + 1}/{DAILY_CLIP_QUOTA} is reserved for exploration. "
            f"Avoid the winning style ({best_style['value']}) to test what else could work."
        )
    else:
        intent["mode"] = "exploit"
        intent["target_style"] = best_style["value"]
        intent["reason"] = (
            f"Slot #{posted_today + 1}/{DAILY_CLIP_QUOTA}: exploit. "
            f"{best_style['value']} clips have done {best_style['lift']:.2f}× baseline."
        )
    logger.info(f"[allocator] camp #{campaign_id}: {intent['reason']}")
    return intent


def render_intent_for_prompt(intent: dict) -> Optional[str]:
    """Turn an intent dict into a paragraph the scorer can paste into its prompt."""
    mode = intent.get("mode")
    if mode == "exploit" and intent.get("target_style"):
        target = intent["target_style"]
        return (
            f"This is an EXPLOIT clip. Strongly prefer moments whose dominant style is "
            f"{target!r} (e.g. matching how the prior winning clips are structured). "
            "If no moment fits that style cleanly, pick the closest match."
        )
    if mode == "explore":
        avoid = intent.get("avoid_styles") or []
        if avoid:
            avoid_str = ", ".join(repr(s) for s in avoid)
            return (
                f"This is an EXPLORE clip — we are testing what ELSE works for this "
                f"campaign. AVOID picking a moment whose style is {avoid_str}. "
                "Pick a moment in a different style to expand our learnings."
            )
        return (
            "This is an EXPLORE clip — we don't have enough data yet to favor a "
            "specific style. Pick whatever you think will perform, and we'll learn from it."
        )
    return None


def _best_content_style(learnings: dict) -> Optional[dict]:
    """Return the best winner whose feature is content_type, or None."""
    winners = learnings.get("winners") or []
    for w in winners:
        if w.get("feature") != "content_type":
            continue
        if (w.get("n") or 0) < MIN_OBSERVATIONS_TO_EXPLOIT:
            continue
        if (w.get("lift") or 0) < MIN_LIFT_TO_EXPLOIT:
            continue
        return w
    return None
