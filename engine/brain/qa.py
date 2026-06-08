"""Quality Assurance — the closed-loop gate.

Runs AFTER the clip is fully produced (final mp4 + final caption + final
hashtags) and BEFORE it goes to the Telegram approval queue or publisher.

Three layers:

  1. MECHANICAL  — reuses `publisher.rule_validator` for hashtag /
                   mention / duration / forbidden-phrase checks.
  2. BRIEF MATCH — does the clip and caption match the Director's brief
                   for this campaign (winning_angle, info_must_include,
                   info_avoid, caption_voice)? Claude-judged.
  3. REJECTION RISK — would a Whop / Clipify reviewer reject this?
                      Claude simulates the reviewer with the campaign's
                      full rules + brief in context.

Output: QAResult { ok: bool, severity: 'block'|'warn'|'fine',
                   issues: [str], suggestions: [str], confidence: 0..1 }

If `ok=False severity=block`, the orchestrator refuses to send the post
to approval AND auto-revises the caption (or re-runs the moment pick)
when possible. If `ok=True` with warnings, they are bundled into the
Telegram approval message so Chris sees them before approving.

This module is the final lever that lets us aim for *zero rejections*.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from .director import get_brief


@dataclass
class QAResult:
    ok: bool
    severity: str  # "block" | "warn" | "fine"
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    layer_results: dict[str, dict] = field(default_factory=dict)
    vision_findings: list[str] = field(default_factory=list)

    def to_html(self) -> str:
        icon = {"block": "🛑", "warn": "⚠️", "fine": "✅"}.get(self.severity, "•")
        lines = [f"{icon} <b>Brain QA — {self.severity.upper()}</b>"]
        if self.issues:
            lines.append("\n<b>Issues:</b>")
            for i in self.issues:
                lines.append(f"  • {i}")
        if self.suggestions:
            lines.append("\n<b>Suggestions:</b>")
            for s in self.suggestions:
                lines.append(f"  → {s}")
        return "\n".join(lines)


def qa_clip(
    repo: Repository,
    campaign: dict,
    *,
    final_caption: str,
    hashtags: list[str],
    transcript_excerpt: str,
    duration_sec: float,
    platforms: list[str],
    hook_text: Optional[str] = None,
    allowed_platforms_override: Optional[list[str]] = None,
    video_path: Optional[str] = None,
) -> QAResult:
    """Run all three QA layers against a fully-produced clip's metadata."""
    issues: list[str] = []
    suggestions: list[str] = []
    layer_results: dict[str, dict] = {}

    # ----- Layer 1: mechanical -----
    full_caption = final_caption + (
        "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags) if hashtags else ""
    )
    structured = _safe_json(campaign.get("structured_rules")) or {}
    plat_required = allowed_platforms_override or structured.get("platforms_required") or []

    mech_issues = []
    try:
        from publisher.rule_validator import validate as validate_rules
        for plat in platforms:
            check = validate_rules(
                caption=full_caption,
                duration_sec=duration_sec,
                platform=plat,
                campaign_rules=campaign.get("rules"),
                platforms_required=plat_required,
                min_duration_sec=campaign.get("min_duration_sec"),
                max_duration_sec=campaign.get("max_duration_sec"),
            )
            for f in check.failures:
                mech_issues.append(f"[{plat}] {f}")
    except Exception as e:
        logger.warning(f"[qa] mechanical layer failed: {e}")

    layer_results["mechanical"] = {"issues": mech_issues}
    if mech_issues:
        issues.extend(mech_issues)
        suggestions.append("Mechanical rule check failed — re-check required hashtags / mentions / duration.")

    # ----- Layer 2: brief match -----
    brief = get_brief(repo, campaign["id"])
    if brief:
        layer_results["brief_match"] = _check_brief_match(
            brief, final_caption, hashtags, hook_text or "", transcript_excerpt
        )
        for i in layer_results["brief_match"].get("issues", []):
            issues.append(f"brief: {i}")
        for s in layer_results["brief_match"].get("suggestions", []):
            suggestions.append(s)

    # ----- Layer 2.5: vision check on the final clip's frames -----
    vision_findings: list[str] = []
    if video_path:
        try:
            vision_findings = _vision_review_clip(video_path, brief, campaign)
            if vision_findings:
                layer_results["vision"] = {"findings": vision_findings}
                # Vision concerns are warnings, not blocks, unless they're
                # severe (the helper prefixes severe issues with "BLOCK:").
                for vf in vision_findings:
                    if vf.startswith("BLOCK:"):
                        issues.append(f"vision: {vf[6:].strip()}")
                    else:
                        suggestions.append(f"vision: {vf}")
        except Exception as e:
            logger.warning(f"[qa] vision review failed: {e}")

    # ----- Layer 3: rejection-risk simulation -----
    risk = _simulate_reviewer(
        campaign, brief, final_caption, hashtags,
        transcript_excerpt, duration_sec, platforms,
    )
    layer_results["rejection_risk"] = risk
    if risk.get("reject_risk", 0) >= 0.5:
        issues.append(f"Reviewer-rejection risk {risk['reject_risk']:.0%}: {risk.get('reason', '')}")
        if risk.get("fix"):
            suggestions.append(risk["fix"])

    # ----- Aggregate severity -----
    if mech_issues or risk.get("reject_risk", 0) >= 0.7:
        severity = "block"
        ok = False
    elif issues:
        severity = "warn"
        ok = True
    else:
        severity = "fine"
        ok = True

    return QAResult(
        ok=ok,
        severity=severity,
        issues=issues,
        suggestions=suggestions,
        confidence=float(risk.get("confidence", 0.5)),
        layer_results=layer_results,
        vision_findings=vision_findings,
    )


# ----------------------------------------------------------------------
def _check_brief_match(
    brief: dict,
    caption: str,
    hashtags: list[str],
    hook_text: str,
    transcript_excerpt: str,
) -> dict:
    """Heuristic + Claude-light check that the produced clip matches the brief."""
    issues: list[str] = []
    suggestions: list[str] = []

    # info_must_include — every entry should appear (case-insensitive) somewhere
    # in caption, hashtags, or hook
    haystack = " ".join([caption, " ".join(hashtags or []), hook_text or ""]).lower()
    for must in brief.get("info_must_include") or []:
        # Soft match: any meaningful word from the "must" string appears
        words = [w for w in re.findall(r"\w+", must.lower()) if len(w) > 3]
        if words and not any(w in haystack for w in words):
            issues.append(f"Missing must-include element: '{must}'")
            suggestions.append(f"Add '{must}' to the caption or hashtags before posting.")

    # info_avoid — semantic concepts to avoid. Single word matches were
    # producing way too many false positives (every English word in the
    # avoid PHRASE flagged any caption containing it). Now: require a
    # contiguous 3-word substring of the avoid phrase to actually appear,
    # which is a much higher bar that catches real violations only.
    for avoid in brief.get("info_avoid") or []:
        avoid_words = re.findall(r"\w+", avoid.lower())
        if len(avoid_words) < 3:
            # For short avoid terms, require the whole phrase to appear.
            if avoid.lower() in haystack:
                issues.append(f"Contains avoid term '{avoid}'")
                suggestions.append(f"Rewrite caption to remove '{avoid}'.")
            continue
        # For longer phrases, look for any 3-gram substring.
        hit = None
        for i in range(len(avoid_words) - 2):
            tri = " ".join(avoid_words[i : i + 3])
            if tri in haystack:
                hit = tri
                break
        if hit:
            issues.append(f"Contains avoid concept '{hit}' (from rule: '{avoid}')")
            suggestions.append(f"Rewrite caption to avoid: '{avoid}'.")
    return {"issues": issues, "suggestions": suggestions}


_REVIEWER_PROMPT = """You are simulating a strict campaign reviewer for a clipping marketplace (Whop / Clipify-style). Your job: decide if THIS proposed clip would be REJECTED for any reason.

Common reasons reviewers reject:
- Missing required hashtags / mentions / brand tags
- Off-topic or misleading caption
- Sensitive / forbidden content (politics, NSFW, misleading health claims, ungrounded financial claims)
- Wrong platform (e.g. posted to YouTube when campaign is TikTok-only)
- Clip too short / too long
- Watermark / low quality / poor framing
- Duplicates an existing approved submission
- Copyrighted music without license

You are given the CAMPAIGN RULES and the CLIP DETAILS. Be a strict-but-realistic reviewer — most clips that follow the rules pass. Don't fail on minor stylistic preferences.

Return ONLY JSON:
{
  "reject_risk": 0.0-1.0,
  "reason": "<short explanation if risk >= 0.3, else empty>",
  "fix": "<one concrete change that would lower the risk, or empty>",
  "confidence": 0.0-1.0
}

CAMPAIGN:
__CAMPAIGN__

DIRECTOR'S BRIEF:
__BRIEF__

CLIP:
__CLIP__
"""


def _simulate_reviewer(
    campaign: dict,
    brief: Optional[dict],
    caption: str,
    hashtags: list[str],
    transcript_excerpt: str,
    duration_sec: float,
    platforms: list[str],
) -> dict:
    if not settings.anthropic_api_key:
        return {"reject_risk": 0.0, "reason": "", "fix": "", "confidence": 0.0}
    try:
        import anthropic
    except ImportError:
        return {"reject_risk": 0.0, "reason": "", "fix": "", "confidence": 0.0}
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    camp_blob = {
        "title": campaign.get("title"),
        "marketplace": campaign.get("marketplace") or "whop",
        "payout_per_1k_views": campaign.get("payout_per_1k_views"),
        "min_duration_sec": campaign.get("min_duration_sec"),
        "max_duration_sec": campaign.get("max_duration_sec"),
        "platforms_required": _safe_json(campaign.get("platforms_required")),
        "structured_rules": _safe_json(campaign.get("structured_rules")),
        "campaign_brief": (campaign.get("campaign_brief") or campaign.get("rules") or "")[:2500],
    }
    clip_blob = {
        "caption": caption[:600],
        "hashtags": hashtags[:15],
        "transcript_first_300": transcript_excerpt[:300],
        "duration_sec": duration_sec,
        "platforms_being_posted_to": platforms,
    }
    prompt = (_REVIEWER_PROMPT
              .replace("__CAMPAIGN__", json.dumps(camp_blob, indent=2, default=str)[:4000])
              .replace("__BRIEF__", json.dumps(brief or {}, indent=2, default=str)[:1500])
              .replace("__CLIP__", json.dumps(clip_blob, indent=2, default=str)[:2000]))
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[qa] reviewer Claude call failed: {e}")
        return {"reject_risk": 0.0, "reason": "", "fix": "", "confidence": 0.0}
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"reject_risk": 0.0, "reason": "", "fix": "", "confidence": 0.0}
    return {
        "reject_risk": min(1.0, max(0.0, float(obj.get("reject_risk", 0.0)))),
        "reason": str(obj.get("reason", ""))[:300],
        "fix": str(obj.get("fix", ""))[:300],
        "confidence": min(1.0, max(0.0, float(obj.get("confidence", 0.5)))),
    }


_VISION_PROMPT = (
    "You are reviewing 3 frames from a finished short-form video clip "
    "that's about to be posted to YouTube Shorts / Instagram Reels. "
    "Identify any QUALITY issues a reviewer would flag, in priority order:\n"
    "- Subject (face/group) covered by burnt-in captions or hook overlay\n"
    "- Subject cropped poorly (cut off head, off-center awkwardly)\n"
    "- Visible watermark from another platform (e.g. 'TikTok' logo bottom-right)\n"
    "- Text overlay illegible (too small, too low contrast, off-screen)\n"
    "- Frame composition obviously broken (huge blur fill, black bars, distortion)\n"
    "- Anything visually misleading for the campaign's content (per brief)\n\n"
    "Brief context (winning angle, must-include, avoid):\n__BRIEF__\n\n"
    'Return ONLY a JSON array of findings. Each finding is a string. '
    'Prefix severe issues with "BLOCK: " — those will halt the post. '
    'Use no prefix for cosmetic / soft suggestions. '
    'If everything looks fine, return [].\n\n'
    "Example: [\"BLOCK: hook overlay covers the speaker's face\", \"caption text could be larger\"]"
)


def _vision_review_clip(video_path: str, brief: Optional[dict], campaign: dict) -> list[str]:
    """Sample 3 frames from the final clip and ask Claude to flag visual issues."""
    if not settings.anthropic_api_key:
        return []
    try:
        import anthropic
    except ImportError:
        return []
    from pathlib import Path as _P
    import base64, subprocess, tempfile, json as _json
    p = _P(video_path)
    if not p.exists():
        return []

    # Sample 3 frames at 10%, 50%, 90% of duration.
    try:
        r = subprocess.run(
            [settings.ffprobe_path, "-loglevel", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=15,
        )
        dur = float(r.stdout.strip())
    except Exception:
        return []
    if dur <= 0:
        return []
    samples = [dur * f for f in (0.1, 0.5, 0.9)]

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    img_blocks = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(samples):
            out = _P(tmp) / f"vf_{i}.jpg"
            try:
                subprocess.run(
                    [settings.ffmpeg_path, "-y", "-loglevel", "error",
                     "-ss", str(ts), "-i", str(p),
                     "-frames:v", "1", "-q:v", "3", "-vf", "scale=540:-2", str(out)],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                continue
            if not out.exists():
                continue
            with open(out, "rb") as fh:
                b64 = base64.standard_b64encode(fh.read()).decode("ascii")
            img_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })

    if not img_blocks:
        return []

    brief_blob = _json.dumps(brief or {}, default=str)[:1200]
    prompt = _VISION_PROMPT.replace("__BRIEF__", brief_blob)
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": img_blocks + [{"type": "text", "text": prompt}],
            }],
        )
    except Exception as e:
        logger.warning(f"[qa][vision] call failed: {e}")
        return []
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        findings = _json.loads(cleaned)
    except Exception:
        return []
    if not isinstance(findings, list):
        return []
    return [str(f)[:300] for f in findings if str(f).strip()]


def _safe_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
