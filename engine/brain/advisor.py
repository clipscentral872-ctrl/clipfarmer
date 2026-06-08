"""Render learned per-campaign patterns into a short string the scorer
can inject into its prompt.

Two surfaces:
- `advice_for_campaign(repo, campaign_id) -> str | None`
  Returns a ready-to-paste prompt block or None if nothing learned yet.
- `human_summary(repo, campaign_id) -> str`
  Returns a Telegram-friendly summary for chat queries like "what has
  the brain learned about #43?"
"""

from __future__ import annotations

from typing import Optional

from db.repository import Repository
from .learnings import get_learnings
from .competitor_learner import get_competitor_insights


# How many winning patterns to inject. Too many = noise.
MAX_WINNERS_IN_PROMPT = 4


_FEATURE_PHRASES = {
    "duration_bucket": {
        "u35": "under 35 seconds",
        "35-45": "in the 35–45 second range",
        "45-55": "in the 45–55 second range",
        "55+": "over 55 seconds long",
    },
    "ai_score_bucket": {
        "90+": "scored 90+ by the scorer",
        "80-90": "scored 80–90 by the scorer",
        "70-80": "scored 70–80 by the scorer",
        "u70": "scored under 70 by the scorer",
    },
    "hashtag_bucket": {
        "u3": "with 0–2 hashtags",
        "3-4": "with 3–4 hashtags",
        "5+": "with 5+ hashtags",
    },
    "hook_style": {
        "question": "opening with a question hook",
        "wh-open": "opening with a how/why/what hook",
        "exclaim": "opening with an exclamation hook",
        "statement": "opening with a flat statement hook",
        "none": "with no hook overlay",
    },
    "time_of_day": {
        "et-morning": "posted in the US morning (5am-11am ET)",
        "et-midday": "posted around US midday (11am-2pm ET)",
        "et-afternoon": "posted in the US afternoon (2pm-6pm ET)",
        "et-evening": "posted in the US evening (6pm-10pm ET)",
        "et-night": "posted in the US late night (10pm-5am ET)",
        "unknown": "with unknown post time",
    },
    "content_type": {
        "person-to-camera": "in a single-speaker-to-camera style",
        "reaction": "in a reaction style (responding to off-screen content)",
        "demonstration": "in a demonstration / how-to style",
        "conversation": "in a conversation / dialogue style",
        "narration": "in a voiceover-narration style",
        "montage": "in a multi-cut montage style",
        "other": "of mixed/other style",
    },
}


def _phrase(feature: str, value: str) -> str:
    return _FEATURE_PHRASES.get(feature, {}).get(value, f"{feature}={value}")


def advice_for_campaign(repo: Repository, campaign_id: int) -> Optional[str]:
    """Short prompt-injectable advice. None if there's not enough data."""
    learnings = get_learnings(repo, campaign_id)
    if not learnings:
        return None
    winners = learnings.get("winners") or []
    plat_recs = learnings.get("platform_recommendations") or []
    if not winners and not plat_recs:
        return None
    baseline = learnings.get("baseline_median_views") or 0
    lines = [
        "What's been working in this campaign so far "
        f"(from {learnings.get('n_clips', 0)} prior clips, "
        f"baseline median {baseline:,} views):",
    ]
    for w in winners[:MAX_WINNERS_IN_PROMPT]:
        phrase = _phrase(w["feature"], w["value"])
        lift = w.get("lift", 1.0)
        med = w.get("median", 0)
        lines.append(
            f"- Clips {phrase} have done {lift:.1f}× the campaign median "
            f"(~{med:,} views over {w.get('n', 0)} clips)."
        )
    # Per-platform — only mention prioritize / drop actions, skip "fine".
    notable = [p for p in plat_recs if p["action"] in ("prioritize", "drop")]
    for p in notable:
        verb = "performs best" if p["action"] == "prioritize" else "underperforms badly"
        lines.append(
            f"- {p['platform'].title()} {verb} for this campaign "
            f"(~{p['median_views']:,} median views over {p['n']} posts)."
        )
    # Competitor signal — what's winning for OTHER creators on this campaign.
    comp = get_competitor_insights(repo, campaign_id)
    if comp:
        comp_bits = []
        styles = comp.get("dominant_styles") or []
        if styles:
            top = styles[0]
            comp_bits.append(f"dominant style is {top['style']}")
        if comp.get("median_duration_sec"):
            comp_bits.append(f"typical length ~{comp['median_duration_sec']}s")
        # Prefer the deep opener phrases (extracted from transcribed audio
        # of competitor winners). Fall back to title-derived ones.
        deep_openers = comp.get("opener_phrases") or []
        meta_openers = comp.get("common_hook_words") or []
        openers = deep_openers or meta_openers
        if openers:
            comp_bits.append(f"common opener phrases: {', '.join(openers[:4])}")
        if comp.get("avg_cuts_per_sec"):
            comp_bits.append(f"avg pacing ~{comp['avg_cuts_per_sec']:.2f} cuts/sec")
        if comp_bits:
            lines.append(
                "- Top performers from competitor clips on this campaign: "
                + "; ".join(comp_bits) + "."
            )
        # Few-shot examples of full competitor openers — most powerful signal
        # for the hook generator. Limited to 3 to keep prompts tight.
        full_examples = comp.get("opener_full_examples") or []
        if full_examples:
            ex_str = "; ".join(f'"{e[:80]}"' for e in full_examples[:3])
            lines.append(f"- Examples of winning competitor first-3-second openers: {ex_str}.")
    lines.append("Lean toward picks and hooks that match these patterns when there's a tie.")
    return "\n".join(lines)


def human_summary(repo: Repository, campaign_id: int) -> str:
    """Telegram-friendly explanation for chat queries."""
    learnings = get_learnings(repo, campaign_id)
    if not learnings:
        return "No learnings yet for this campaign — need at least 3 posted+tracked clips."
    n = learnings.get("n_clips", 0)
    baseline = learnings.get("baseline_median_views", 0)
    out = [f"<b>Brain on this campaign</b> — {n} clips, baseline {baseline:,} views median."]
    winners = learnings.get("winners") or []
    if not winners:
        out.append("No feature has crossed the 1.25× lift threshold yet — needs more data.")
        return "\n".join(out)
    out.append("Winning patterns:")
    for w in winners[:6]:
        phrase = _phrase(w["feature"], w["value"])
        out.append(f"• {phrase} — {w['lift']:.2f}× baseline ({w['median']:,} views, n={w['n']})")
    plat_recs = learnings.get("platform_recommendations") or []
    if plat_recs:
        out.append("")
        out.append("Per-platform:")
        for p in plat_recs:
            tag = {"prioritize": "🔥 prioritize",
                   "drop": "⛔ drop",
                   "fine": "✓ fine"}.get(p["action"], p["action"])
            out.append(f"• {p['platform']}: {tag} — {p['median_views']:,} median views (n={p['n']})")
    return "\n".join(out)
