"""Read a free-form campaign brief and extract structured rules.

Whop campaign briefs vary wildly — sometimes a few bullets on the campaign
card, sometimes a Google Doc with the full PR/legal-vetted shape. We feed
the raw text to Claude and ask for a fixed-shape JSON blob the rest of
the system can rely on (scorer, publisher, validators).

Output shape (every field optional except `summary`):
{
    "summary": "one-sentence description of what the campaign wants",
    "required_caption": "exact caption text the post must use, or null",
    "caption_handling": "exact | starts_with | contains | none",
    "required_hashtags": ["#tag", ...],
    "required_mentions": ["@brand", ...],
    "forbidden_phrases": ["AI", "Peter Thiel", ...],
    "platforms_required": ["tiktok","youtube","instagram","x"],
    "source_must_match": ["wetransfer.com/...", "drive.google.com/..."],
    "min_seconds": 30, "max_seconds": 90,
    "aspect_ratio": "9:16" | "16:9" | "either",
    "treatment_notes": "free-form notes the scorer should pass to Claude as guidance",
    "do_list":   ["..."],
    "dont_list": ["..."]
}
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from loguru import logger

from config import settings


class RulesExtractionError(RuntimeError):
    pass


def extract_rules(
    brief_text: str,
    campaign_title: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    """Run Claude over the brief text and return a structured rules dict.

    Raises RulesExtractionError on API failure or unparseable response.
    """
    if not brief_text or not brief_text.strip():
        raise RulesExtractionError("brief_text is empty")

    api_key = settings.anthropic_api_key
    if not api_key:
        raise RulesExtractionError("ANTHROPIC_API_KEY not set")
    model = model or settings.anthropic_model

    try:
        from engine import llm_compat as anthropic
    except ImportError as e:
        raise RulesExtractionError("anthropic SDK not installed") from e

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(brief_text, campaign_title)
    logger.info(f"[rules] extracting structured rules via {model} ({len(brief_text)} chars)")

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2_000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RulesExtractionError(f"Claude call failed: {e}") from e

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_response(text)


# ----------------------------------------------------------------------
def _build_prompt(brief_text: str, campaign_title: Optional[str]) -> str:
    title_line = f"Campaign title: {campaign_title}\n\n" if campaign_title else ""
    return f"""You are reading a Whop Content Rewards campaign brief and extracting the rules a clip-creation pipeline must follow.

{title_line}Brief:
\"\"\"
{brief_text.strip()[:20_000]}
\"\"\"

Return ONLY a JSON object with this exact shape (every field optional except `summary`; use null for fields the brief does not specify; use empty arrays where appropriate):

{{
  "summary": "one-sentence description of what the campaign wants",
  "required_caption": "the exact caption text the post must use verbatim, or null if no exact caption is required",
  "caption_handling": "exact | starts_with | contains | none",
  "required_hashtags": ["#tag1", "#tag2"],
  "required_mentions": ["@brand"],
  "forbidden_phrases": ["phrases that must not appear in the clip or caption"],
  "platforms_required": ["tiktok","youtube","instagram","x"],
  "source_must_match": ["substrings the source video URL must contain, e.g. 'wetransfer.com'"],
  "min_seconds": null,
  "max_seconds": null,
  "aspect_ratio": "9:16",
  "treatment_notes": "free-form notes about how the clip should look/feel — pass-through guidance for the moment-picker (e.g. 'protect the talent, no out-of-context mocking', 'big captions white text black outline', 'hook in first 2 seconds')",
  "do_list":   ["short DO bullets pulled from the brief"],
  "dont_list": ["short DON'T bullets pulled from the brief"],

  "tracking_code_required": false,
  "tracking_code_location": "description | caption | none",

  "analytics": {{
    "required": false,
    "format": "screenshot | screenrecording | none",
    "delivery_channel": "support_chat | google_form | telegram | none",
    "delivery_url": "the exact URL the brief gives for analytics delivery, or null",
    "due_after_hours": null,
    "required_elements": ["views", "country breakdown", "age breakdown", "watch time", "etc"]
  }},

  "submission": {{
    "form_fields": ["title", "video link", "demographics image", "etc — anything the brief says the SUBMIT form will ask for"],
    "demographics_image_required": false,
    "extra_requirements": ["other rules about submission, e.g. 'video must be live 24h before submitting'"]
  }}
}}

Rules for the extraction:
- If the brief says \"use this exact caption\" or \"copy-paste this caption\", put it under `required_caption` and set `caption_handling`: \"exact\".
- If the brief lists hashtags or @ mentions that must be on every post, put them under `required_hashtags` / `required_mentions`.
- If the brief tells you NOT to mention certain things (e.g. \"no AI mention\", \"don't name Peter Thiel\"), put each forbidden phrase as a SHORT lowercase string in `forbidden_phrases` so it can be matched against transcripts (e.g. [\"ai\", \"peter thiel\"]).
- `platforms_required` should ONLY include platforms the brief explicitly lists as accepted. If unspecified, return [].
- `source_must_match` lists substrings the source URL must contain. If the brief says 'USE ONLY THE FOOTAGE WE PROVIDE' and links to wetransfer.com, put 'wetransfer.com' there.
- Keep `treatment_notes` to under 400 characters — concise pipeline guidance only.
- For `analytics`: read carefully. If the brief explicitly says screen RECORDING (a video walkthrough), set `format`: \"screenrecording\". If it says screenshot or doesn't specify, default to \"screenshot\" when analytics are required. If the brief doesn't mention analytics at all, set `required`: false and `format`: \"none\". Look for explicit time windows like \"48 hours after\", \"after 2 days\", \"once live for 24h\" — put the integer in `due_after_hours`. Look for explicit delivery channels: \"send to support chat\", \"submit via this Google Form\", etc. If the brief gives a URL like a Google Form or a chat URL, capture it verbatim in `delivery_url`.
- For `submission`: list every field the brief explicitly says the Whop submission form will ask for (e.g. 'Title', 'Video Link', 'Demographics Image', etc).
- For `tracking_code_required`: true if the brief says you must include a tracking code in the post description (the code Whop returns after submission). Otherwise false.
"""


def _parse_response(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0:
        raise RulesExtractionError(f"Claude returned no JSON object. First 300 chars: {text[:300]}")
    try:
        obj = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        raise RulesExtractionError(f"JSON parse failed: {e}\n{cleaned[start:end+1][:500]}")
    if not isinstance(obj, dict) or "summary" not in obj:
        raise RulesExtractionError("response missing required 'summary' field")
    return _normalize(obj)


def _normalize(obj: dict[str, Any]) -> dict[str, Any]:
    """Coerce types and drop obviously-empty values."""
    def _list_of_str(v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()]

    obj["required_hashtags"]  = _list_of_str(obj.get("required_hashtags"))
    obj["required_mentions"]  = _list_of_str(obj.get("required_mentions"))
    obj["forbidden_phrases"]  = [p.lower() for p in _list_of_str(obj.get("forbidden_phrases"))]
    obj["platforms_required"] = [p.lower() for p in _list_of_str(obj.get("platforms_required"))]
    obj["source_must_match"]  = _list_of_str(obj.get("source_must_match"))
    obj["do_list"]            = _list_of_str(obj.get("do_list"))
    obj["dont_list"]          = _list_of_str(obj.get("dont_list"))

    # Nested analytics block — make sure it always exists with sensible defaults.
    a = obj.get("analytics") or {}
    if not isinstance(a, dict):
        a = {}
    a["required"]         = bool(a.get("required") or False)
    a["format"]           = (a.get("format") or "none").lower()
    a["delivery_channel"] = (a.get("delivery_channel") or "none").lower()
    a["delivery_url"]     = a.get("delivery_url") or None
    a["due_after_hours"]  = a.get("due_after_hours") if isinstance(a.get("due_after_hours"), (int, float)) else None
    a["required_elements"] = _list_of_str(a.get("required_elements"))
    obj["analytics"] = a

    # Submission block.
    s = obj.get("submission") or {}
    if not isinstance(s, dict):
        s = {}
    s["form_fields"] = _list_of_str(s.get("form_fields"))
    s["demographics_image_required"] = bool(s.get("demographics_image_required") or False)
    s["extra_requirements"] = _list_of_str(s.get("extra_requirements"))
    obj["submission"] = s

    obj["tracking_code_required"] = bool(obj.get("tracking_code_required") or False)
    obj["tracking_code_location"] = (obj.get("tracking_code_location") or "none").lower()

    return obj
