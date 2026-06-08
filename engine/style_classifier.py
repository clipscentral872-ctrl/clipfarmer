"""Tag every produced clip with its content style.

Used by the Brain to learn which styles win per campaign — and by the
exploit/explore allocator to plan a multi-clip batch as "2 in the
winning style + 1 experimental".

Style is a tiny fixed vocabulary, chosen to be:
- Stable (categories don't shift over time)
- Distinguishable from transcript alone (no vision call needed)
- Meaningful for short-form social video

Categories:
    person-to-camera : single speaker addressing camera directly ("I", "you", direct address)
    reaction         : responding to off-screen content ("look at this", "no way", "oh my god")
    demonstration    : showing how to do or use something ("watch", "step 1", "here's how")
    conversation     : two or more named/distinct speakers interacting
    narration        : voiceover over external footage (third-person tense, descriptive)
    montage          : multiple short cuts strung together (rapid transitions, no through-line)
    other            : doesn't fit cleanly

We classify using Claude on the transcript excerpt + hook — no vision
call, keeps cost trivial.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from loguru import logger

from config import settings


CONTENT_TYPES = (
    "person-to-camera",
    "reaction",
    "demonstration",
    "conversation",
    "narration",
    "montage",
    "other",
)


_PROMPT_TEMPLATE = (
    "You are tagging the *style* of a short-form video clip for a learning system.\n\n"
    "Pick exactly ONE label from this set:\n"
    "- person-to-camera : a single speaker addressing the camera directly (uses \"I\" / \"you\", looks at viewer)\n"
    "- reaction         : someone responding to off-screen content (e.g. reacting to a video, watching something)\n"
    "- demonstration    : showing how to do or use something (recipes, walkthroughs, product showcases)\n"
    "- conversation     : two or more distinct speakers interacting (interview, podcast back-and-forth)\n"
    "- narration        : voiceover describing external footage (third-person, descriptive)\n"
    "- montage          : multiple short cuts strung together without one through-line\n"
    "- other            : doesn't fit cleanly\n\n"
    'Return ONLY a JSON object: {"style": "<label>", "confidence": 0.0-1.0, "reason": "<one short sentence>"}\n\n'
    "Hook text (first 3 seconds on-screen): __HOOK__\n"
    "Transcript excerpt:\n__TRANSCRIPT__\n"
)


def classify_clip(
    transcript_excerpt: str,
    hook_text: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Return {style, confidence, reason} or None on failure."""
    if not transcript_excerpt or len(transcript_excerpt.strip()) < 20:
        # Too little text → can't classify reliably.
        return None
    if not settings.anthropic_api_key:
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("[style] anthropic SDK missing")
        return None

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _PROMPT_TEMPLATE.replace(
        "__HOOK__", (hook_text or "(none)")[:200]
    ).replace(
        "__TRANSCRIPT__", transcript_excerpt[:2000]
    )
    try:
        resp = client.messages.create(
            model=model or settings.anthropic_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[style] Claude call failed: {e}")
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"[style] unparseable JSON: {text[:200]}")
        return None
    style = (obj.get("style") or "").strip().lower()
    if style not in CONTENT_TYPES:
        logger.warning(f"[style] invalid style {style!r}; falling back to 'other'")
        style = "other"
    return {
        "style": style,
        "confidence": float(obj.get("confidence") or 0.0),
        "reason": (obj.get("reason") or "").strip()[:200],
    }
