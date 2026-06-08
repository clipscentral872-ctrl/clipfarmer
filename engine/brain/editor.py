"""Brain as editor — auto-revise clip text to pass QA and lift quality.

Two surfaces:

  1. `revise_for_approval(...)` — runs after QA blocks a clip. Takes the
     QA issues + the Director's brief + the original caption/hashtags/hook
     and regenerates ONLY the failing piece. Re-checks QA. Loops up to
     MAX_REVISIONS attempts.

  2. `polish_for_quality(...)` — runs even when QA passes. Asks Claude
     to suggest an improved caption/hook based on the campaign's learned
     winning patterns + competitor opener phrases. Returns the polished
     version only if it's *meaningfully* different (not cosmetic).

Both produce new {caption, hashtags, hook_text} triples without
re-cutting the video — so they're cheap and safe to run inline before
the Telegram approval message goes out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from .advisor import advice_for_campaign
from .director import get_brief
from .qa import qa_clip, QAResult


MAX_REVISIONS = 3


@dataclass
class Revision:
    caption: str
    hashtags: list[str]
    hook_text: str
    notes: str
    attempts: int
    final_qa: Optional[QAResult] = None


def revise_for_approval(
    repo: Repository,
    campaign: dict,
    *,
    caption: str,
    hashtags: list[str],
    hook_text: str,
    transcript_excerpt: str,
    duration_sec: float,
    platforms: list[str],
    qa_result: QAResult,
) -> Optional[Revision]:
    """Revise the clip's text fields until QA passes (or attempts exhausted).
    Returns Revision on success, None if Brain couldn't fix it."""
    cur_caption = caption
    cur_hashtags = list(hashtags or [])
    cur_hook = hook_text or ""
    notes_log: list[str] = []
    last_qa = qa_result

    for attempt in range(1, MAX_REVISIONS + 1):
        if last_qa.severity != "block":
            break
        edit = _ask_claude_to_edit(
            repo, campaign,
            caption=cur_caption, hashtags=cur_hashtags, hook_text=cur_hook,
            qa_issues=last_qa.issues, qa_suggestions=last_qa.suggestions,
        )
        if not edit:
            notes_log.append(f"attempt {attempt}: editor returned nothing")
            break
        cur_caption = edit.get("caption", cur_caption)
        cur_hashtags = edit.get("hashtags", cur_hashtags)
        cur_hook = edit.get("hook_text", cur_hook)
        notes_log.append(f"attempt {attempt}: {edit.get('change_summary', '')}")
        last_qa = qa_clip(
            repo, campaign,
            final_caption=cur_caption, hashtags=cur_hashtags,
            transcript_excerpt=transcript_excerpt, duration_sec=duration_sec,
            platforms=platforms, hook_text=cur_hook,
        )
        logger.info(
            f"[editor] revision {attempt}: severity={last_qa.severity}, "
            f"issues={len(last_qa.issues)}"
        )

    if last_qa.severity == "block":
        return None
    return Revision(
        caption=cur_caption,
        hashtags=cur_hashtags,
        hook_text=cur_hook,
        notes=" | ".join(notes_log),
        attempts=attempt,
        final_qa=last_qa,
    )


def polish_for_quality(
    repo: Repository,
    campaign: dict,
    *,
    caption: str,
    hashtags: list[str],
    hook_text: str,
    transcript_excerpt: str,
) -> Optional[dict]:
    """Suggest a higher-quality caption/hook based on learned + competitor
    patterns. Returns {caption, hashtags, hook_text, why} or None if the
    current version is already optimal / Claude declines to change anything."""
    advice = advice_for_campaign(repo, campaign["id"]) or ""
    brief = get_brief(repo, campaign["id"]) or {}
    if not advice and not brief:
        return None

    edit = _ask_claude_to_polish(
        repo, campaign,
        caption=caption, hashtags=hashtags, hook_text=hook_text,
        transcript_excerpt=transcript_excerpt,
        advice=advice, brief=brief,
    )
    if not edit:
        return None
    # Only return if the polished version is meaningfully different.
    if _meaningfully_different(caption, edit.get("caption", "")) or \
       _meaningfully_different(hook_text, edit.get("hook_text", "")):
        return edit
    return None


# ----------------------------------------------------------------------
_EDIT_PROMPT = """You are the editor in a closed-loop clipping system. The QA system flagged the clip below as RISKY for review-rejection. Your job: REVISE only what's necessary (caption, hashtags, or hook text) so the clip would pass review while staying as close to the original spirit as possible.

Constraints:
- Do NOT change the underlying moment / transcript — only caption, hashtags, and hook overlay text.
- Honor every required hashtag / mention / brand tag the campaign demands.
- Address each QA issue listed below.
- Match the Director's brief (winning_angle, info_must_include, info_avoid, caption_voice) if present.
- Be concise — captions ≤ 200 chars unless campaign requires longer.

Return ONLY JSON:
{
  "caption": "<new caption>",
  "hashtags": ["tag1", "tag2", ...],
  "hook_text": "<new hook (≤60 chars)>",
  "change_summary": "<one short sentence: what changed and why>"
}

CAMPAIGN:
__CAMPAIGN__

DIRECTOR'S BRIEF:
__BRIEF__

QA ISSUES TO FIX:
__ISSUES__

ORIGINAL CLIP:
__ORIGINAL__
"""


def _ask_claude_to_edit(
    repo: Repository,
    campaign: dict,
    *,
    caption: str,
    hashtags: list[str],
    hook_text: str,
    qa_issues: list[str],
    qa_suggestions: list[str],
) -> Optional[dict]:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    brief = get_brief(repo, campaign["id"]) or {}

    camp_blob = {
        "title": campaign.get("title"),
        "structured_rules": _safe_json(campaign.get("structured_rules")),
        "campaign_brief": (campaign.get("campaign_brief") or campaign.get("rules") or "")[:1500],
    }
    original_blob = {
        "caption": caption,
        "hashtags": hashtags,
        "hook_text": hook_text,
    }
    issues_blob = {"issues": qa_issues, "suggestions": qa_suggestions}

    prompt = (_EDIT_PROMPT
              .replace("__CAMPAIGN__", json.dumps(camp_blob, indent=2, default=str)[:3000])
              .replace("__BRIEF__", json.dumps(brief, indent=2, default=str)[:1500])
              .replace("__ISSUES__", json.dumps(issues_blob, indent=2)[:1500])
              .replace("__ORIGINAL__", json.dumps(original_blob, indent=2, default=str)[:1500]))

    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[editor] Claude edit call failed: {e}")
        return None
    return _parse_edit_response(resp)


_POLISH_PROMPT = """You are the quality editor in a closed-loop clipping system. The clip below passed QA, but you have access to what HAS worked for this campaign (learned patterns) + what competitor winners are doing. Your job: suggest a meaningfully BETTER caption / hook if you can — one that exploits the learned + competitor patterns without changing the underlying moment.

If the original is already strong, OR the patterns suggest no clear improvement, return the original unchanged.

Constraints:
- Keep all required hashtags + mentions intact.
- Hook ≤ 60 chars, on-screen overlay style.
- Caption ≤ 200 chars unless campaign needs more.
- Honor the Director's brief if present (winning_angle, info_must_include, info_avoid, caption_voice).

Return ONLY JSON:
{
  "caption": "<polished or original caption>",
  "hashtags": ["tag1", ...],
  "hook_text": "<polished or original hook>",
  "why": "<short explanation: what insight you applied, or 'no improvement'>"
}

CAMPAIGN:
__CAMPAIGN__

DIRECTOR'S BRIEF + LEARNED PATTERNS + COMPETITOR SIGNAL:
__ADVICE__

CLIP'S TRANSCRIPT:
__TRANSCRIPT__

ORIGINAL CLIP TEXT:
__ORIGINAL__
"""


def _ask_claude_to_polish(
    repo: Repository,
    campaign: dict,
    *,
    caption: str,
    hashtags: list[str],
    hook_text: str,
    transcript_excerpt: str,
    advice: str,
    brief: dict,
) -> Optional[dict]:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    advice_blob = {"brief": brief, "advice": advice}
    prompt = (_POLISH_PROMPT
              .replace("__CAMPAIGN__", str(campaign.get("title", ""))[:200])
              .replace("__ADVICE__", json.dumps(advice_blob, indent=2, default=str)[:4000])
              .replace("__TRANSCRIPT__", transcript_excerpt[:2000])
              .replace("__ORIGINAL__", json.dumps({
                  "caption": caption, "hashtags": hashtags, "hook_text": hook_text,
              }, indent=2)[:1500]))
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[editor] Claude polish call failed: {e}")
        return None
    return _parse_edit_response(resp)


def _parse_edit_response(resp) -> Optional[dict]:
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"[editor] unparseable: {text[:200]}")
        return None
    if not isinstance(obj, dict):
        return None
    return {
        "caption": str(obj.get("caption", ""))[:1000],
        "hashtags": [str(h)[:60] for h in obj.get("hashtags", []) if str(h).strip()][:15],
        "hook_text": str(obj.get("hook_text", ""))[:120],
        "change_summary": str(obj.get("change_summary", obj.get("why", "")))[:300],
    }


def _meaningfully_different(a: str, b: str) -> bool:
    if not a or not b:
        return False
    aw = set(re.findall(r"\w+", a.lower()))
    bw = set(re.findall(r"\w+", b.lower()))
    if not aw:
        return bool(bw)
    overlap = len(aw & bw) / len(aw | bw) if (aw | bw) else 0
    return overlap < 0.75


def _safe_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
