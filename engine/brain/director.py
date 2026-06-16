"""The Director — pre-pipeline creative brief generator.

When the scanner discovers a new campaign (or any time the campaign's
data changes materially), the Director:

  1. Inspects: brief, CPM, budget, our past learnings (if any),
     competitor top performers (if any), discord/marketplace, structured
     rules (hashtags / required mentions / forbidden phrases).
  2. Decides: GO / CONSIDER / NO — is this campaign worth our time?
  3. If GO/CONSIDER, drafts a CREATIVE BRIEF:
        - camera_angle (close-up / wide / mixed)
        - pacing (fast / medium / slow)
        - music_genre + energy (advisory; actual music overlay is future work)
        - caption_voice (punchy / storytelling / informational)
        - info_must_include  (specific facts / brand mentions / value props)
        - info_avoid         (legal-risky claims, off-brand topics)
        - winning_angle      (the single most likely viral framing)
        - predicted_value_per_clip (USD)

The brief is persisted on the campaign row and is injected into the
scorer's prompt, the captioner, and (later) music selection.

This shifts the Brain from rear-view analyst → forward-looking strategist.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from .learnings import get_learnings
from .competitor_learner import get_competitor_insights


CAMERA_ANGLES = ("close-up", "medium", "wide", "mixed")
PACING_VALUES = ("fast", "medium", "slow")
CAPTION_VOICES = ("punchy", "storytelling", "informational", "conversational")
GO_DECISIONS = ("go", "consider", "no")


_DIRECTOR_PROMPT = """You are the Creative Director for a viral short-form clipping system. Before any clip is produced for a campaign, YOU decide whether it's worth our effort and HOW the content should be shaped.

Your job for the campaign below:

1. DECIDE: should we work on this campaign?
   - "go"        — strong opportunity, start producing now
   - "consider"  — marginal, test 1-2 clips before committing
   - "no"        — skip, the upside doesn't justify production cost

2. If "go" or "consider", draft a CREATIVE BRIEF the producer must follow:
   - camera_angle:   one of [close-up, medium, wide, mixed]
   - pacing:         one of [fast, medium, slow]    (fast = many cuts, slow = long takes)
   - music_genre:    a short label (e.g. "lo-fi hip hop", "cinematic orchestral", "uplifting electronic", "none")
   - music_energy:   one of [high, mid, low]
   - caption_voice:  one of [punchy, storytelling, informational, conversational]
   - info_must_include:   2-5 specific facts / mentions / value props the caption must contain
   - info_avoid:          phrases / topics to NEVER touch (legal, off-brand, played out)
   - winning_angle:       ONE sentence describing the single best viral framing for this campaign
   - predicted_value_per_clip:  expected USD per clip (= median competitor views × CPM / 1000)
   - reasoning:           why this brief specifically — what gap/insight it exploits

Be specific. Generic briefs ("be engaging") are worthless — every field should be tactical enough that a producer with no further context could execute on it.

Return ONLY a JSON object, no prose:
{
  "decision": "go|consider|no",
  "predicted_value_per_clip": <number>,
  "winning_angle": "<one sentence>",
  "camera_angle": "...",
  "pacing": "...",
  "music_genre": "...",
  "music_energy": "...",
  "caption_voice": "...",
  "info_must_include": ["...", "..."],
  "info_avoid": ["...", "..."],
  "reasoning": "<one short paragraph>"
}

Campaign context:
__CONTEXT__
"""


def brief_for_campaign(repo: Repository, campaign_id: int, force: bool = False) -> Optional[dict]:
    """Generate or refresh the creative brief for one campaign. Returns
    the brief dict, or None if we couldn't / shouldn't generate."""
    _ensure_brief_column(repo)
    with repo.conn() as c:
        row = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        return None
    camp = dict(row)

    if not force and camp.get("creative_brief"):
        # Already briefed; only re-brief if structured_rules changed or
        # we have substantially new outcome data.
        return _maybe_load(camp.get("creative_brief"))

    ctx = _build_context(repo, camp)
    brief = _ask_claude_for_brief(ctx)
    if not brief:
        return None
    brief = _validate(brief)
    brief["computed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _persist(repo, campaign_id, brief)
    logger.info(
        f"[director] briefed #{campaign_id} {camp.get('title')!r}: "
        f"{brief['decision']}, ${brief.get('predicted_value_per_clip', 0):.2f}/clip, "
        f"camera={brief.get('camera_angle')}, pacing={brief.get('pacing')}"
    )
    return brief


def get_brief(repo: Repository, campaign_id: int) -> Optional[dict]:
    _ensure_brief_column(repo)
    with repo.conn() as c:
        row = c.execute("SELECT creative_brief FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return _maybe_load(row["creative_brief"]) if row else None


def render_brief_for_scorer(brief: dict) -> str:
    """Compact form for prompt injection alongside other advisor lines."""
    if not brief:
        return ""
    parts = [
        f"Director's brief (follow this for THIS campaign):",
        f"- Winning angle: {brief.get('winning_angle', '?')}",
        f"- Camera: {brief.get('camera_angle')} / Pacing: {brief.get('pacing')}",
        f"- Voice: {brief.get('caption_voice')}",
    ]
    must = brief.get("info_must_include") or []
    if must:
        parts.append(f"- Must include in captions: {', '.join(must)}")
    avoid = brief.get("info_avoid") or []
    if avoid:
        parts.append(f"- Avoid: {', '.join(avoid)}")
    return "\n".join(parts)


def render_brief_html(brief: dict, title: str = "") -> str:
    """Telegram-friendly rendering of the brief."""
    if not brief:
        return ""
    icon = {"go": "🟢", "consider": "🟡", "no": "🔴"}.get(brief.get("decision"), "•")
    lines = [
        f"{icon} <b>Director's brief — {title}</b>",
        f"<b>Decision:</b> {brief.get('decision', '?').upper()}  "
        f"(predicted ${brief.get('predicted_value_per_clip', 0):.2f}/clip)",
        f"<b>Winning angle:</b> {brief.get('winning_angle', '?')}",
        "",
        f"📷 <b>Camera:</b> {brief.get('camera_angle', '?')}",
        f"⏱ <b>Pacing:</b> {brief.get('pacing', '?')}",
        f"🎵 <b>Music:</b> {brief.get('music_genre', '?')} ({brief.get('music_energy', '?')} energy)",
        f"🎙 <b>Caption voice:</b> {brief.get('caption_voice', '?')}",
    ]
    must = brief.get("info_must_include") or []
    if must:
        lines.append(f"\n<b>Must include:</b>")
        for m in must:
            lines.append(f"  ✓ {m}")
    avoid = brief.get("info_avoid") or []
    if avoid:
        lines.append(f"\n<b>Avoid:</b>")
        for a in avoid:
            lines.append(f"  ✗ {a}")
    lines.append(f"\n<i>{brief.get('reasoning', '')}</i>")
    return "\n".join(lines)


def refresh_briefs_and_notify(repo: Repository, only: Optional[int] = None) -> dict[int, dict]:
    """Run the director across active campaigns; Telegram-notify GO decisions."""
    if only is not None:
        ids = [only]
    else:
        with repo.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM campaigns WHERE status='active' OR status IS NULL"
            ).fetchall()]
    out: dict[int, dict] = {}
    notes: list[str] = []
    for cid in ids:
        title = _title(repo, cid)
        brief = brief_for_campaign(repo, cid, force=True)
        if not brief:
            continue
        out[cid] = brief
        # Notify on GO and CONSIDER; quietly skip NO unless force-listing.
        if brief.get("decision") in ("go", "consider"):
            notes.append(render_brief_html(brief, title=f"#{cid} {title}"))
    if notes:
        try:
            from publisher.telegram_gate import TelegramGate
            gate = TelegramGate()
            if gate.enabled:
                for n in notes:
                    gate.notify(n)
        except Exception as e:
            logger.warning(f"[director] telegram notify failed: {e}")
    return out


# ----------------------------------------------------------------------
def _build_context(repo: Repository, camp: dict) -> str:
    # Pull cross-campaign + rejection patterns + reflection-derived
    # corrections so each new brief inherits everything the Brain has
    # learned globally, not just on this campaign.
    try:
        from .cross_pattern import render_for_director as _cross_render
        cross_patterns = _cross_render(repo)
    except Exception:
        cross_patterns = ""
    try:
        from .rejection_learning import render_for_prompt as _reject_render
        rejection_patterns = _reject_render(repo, camp["id"])
    except Exception:
        rejection_patterns = ""
    try:
        from .reflection import get_correction
        ev_correction = get_correction(repo, "director_ev")
    except Exception:
        ev_correction = 1.0

    bits = {
        "campaign_id": camp.get("id"),
        "title": camp.get("title"),
        "marketplace": camp.get("marketplace") or "whop",
        "marketplace_server": camp.get("marketplace_server"),
        "cpm_usd": camp.get("payout_per_1k_views"),
        "budget_remaining_pct": camp.get("budget_remaining_pct"),
        "brief_text": (camp.get("campaign_brief") or camp.get("rules") or "")[:3000],
        "structured_rules": _safe_json(camp.get("structured_rules")),
        "competitor_insights": get_competitor_insights(repo, camp["id"]) or {},
        "our_learnings": get_learnings(repo, camp["id"]) or {},
        "system_wide_patterns": cross_patterns,
        "operator_rejection_patterns": rejection_patterns,
        "your_past_ev_predictions_bias": (
            f"Your past predicted $/clip have been off by a factor of "
            f"{ev_correction:.2f}× on actual outcomes. Adjust your "
            f"predicted_value_per_clip down/up accordingly."
            if abs(ev_correction - 1.0) > 0.15 else ""
        ),
    }
    return json.dumps(bits, indent=2, default=str)[:9000]


def _ask_claude_for_brief(context_json: str) -> Optional[dict]:
    if not settings.anthropic_api_key:
        return None
    try:
        from engine import llm_compat as anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _DIRECTOR_PROMPT.replace("__CONTEXT__", context_json)
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[director] Claude call failed: {e}")
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"[director] unparseable JSON: {text[:300]}")
        return None


def _validate(brief: dict) -> dict:
    """Coerce / sanitize the model's output to the expected shape."""
    def _enum(key: str, allowed: tuple, default: str) -> str:
        v = str(brief.get(key, "") or "").lower().strip()
        return v if v in allowed else default
    out = {
        "decision": _enum("decision", GO_DECISIONS, "consider"),
        "predicted_value_per_clip": _to_float(brief.get("predicted_value_per_clip"), 0.0),
        "winning_angle": str(brief.get("winning_angle", "") or "")[:300],
        "camera_angle": _enum("camera_angle", CAMERA_ANGLES, "mixed"),
        "pacing": _enum("pacing", PACING_VALUES, "medium"),
        "music_genre": str(brief.get("music_genre", "") or "")[:60],
        "music_energy": _enum("music_energy", ("high", "mid", "low"), "mid"),
        "caption_voice": _enum("caption_voice", CAPTION_VOICES, "punchy"),
        "info_must_include": _str_list(brief.get("info_must_include"))[:8],
        "info_avoid": _str_list(brief.get("info_avoid"))[:8],
        "reasoning": str(brief.get("reasoning", "") or "")[:600],
    }
    return out


def _to_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x)[:200] for x in v if str(x).strip()]


def _safe_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _maybe_load(raw):
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _title(repo: Repository, cid: int) -> str:
    with repo.conn() as c:
        row = c.execute("SELECT title FROM campaigns WHERE id=?", (cid,)).fetchone()
    return (row["title"] if row else "(unknown)")[:50]


_BRIEF_COLUMN_CHECKED = False


def _ensure_brief_column(repo: Repository) -> None:
    global _BRIEF_COLUMN_CHECKED
    if _BRIEF_COLUMN_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "creative_brief" not in cols:
            c.execute("ALTER TABLE campaigns ADD COLUMN creative_brief TEXT")
    _BRIEF_COLUMN_CHECKED = True


def _persist(repo: Repository, campaign_id: int, brief: dict) -> None:
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET creative_brief = ? WHERE id = ?",
            (json.dumps(brief), campaign_id),
        )
