"""Learn from rejections.

Currently when Chris taps /reject in Telegram on a clip, we record the
rejection but never learn WHY. That's a wasted signal — rejections are
human-curated labels of "this isn't good enough" that the Brain could
use to:

  - Adjust scorer weights (e.g. statement hooks getting rejected → bias toward question hooks)
  - Update Director's `info_avoid` (rejected captions reveal what Chris dislikes)
  - Tune QA's reviewer simulation (rejected clips reveal real-reviewer triggers)

This module:
  1. Pulls all clips with status='rejected'
  2. Asks Claude to extract recurring rejection patterns
  3. Saves them to a per-campaign `rejection_patterns` JSON
  4. Director's prompt + scorer's prompt consume these so subsequent
     clips avoid the rejected patterns

Triggered nightly + on-demand. Empty no-op if no rejections yet.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository


_REJECT_PROMPT = """You are analyzing rejected video clips to extract patterns. These are clips that the operator chose NOT to post, after seeing them in a Telegram approval prompt. The operator's rejection reason isn't recorded explicitly — you need to INFER WHY each clip was likely rejected, then identify patterns across the rejections.

For each rejected clip, you have:
- the moment's transcript excerpt
- the AI-generated hook overlay
- the AI-generated caption + hashtags
- the AI score

Identify 1-4 patterns that recur across rejections. Be SPECIFIC — not "low quality" but "captions start with all-caps clickbait phrasing the operator finds tacky".

Return ONLY JSON: {"patterns": ["<short pattern>", ...], "evidence_count": N}

REJECTED CLIPS:
__CLIPS__
"""


def learn_from_rejections(repo: Repository, campaign_id: Optional[int] = None) -> dict:
    """Extract per-campaign rejection patterns. Persists to
    `campaigns.rejection_patterns` JSON column."""
    _ensure_column(repo)
    targets = _campaigns_to_analyze(repo, campaign_id)
    out: dict[int, dict] = {}
    for cid in targets:
        rejections = _pull_rejections(repo, cid)
        if len(rejections) < 2:
            continue
        patterns = _ask_claude(rejections)
        if not patterns:
            continue
        payload = {
            "patterns": patterns.get("patterns", []),
            "n_rejections": len(rejections),
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _persist(repo, cid, payload)
        out[cid] = payload
        logger.info(
            f"[reject-learn] #{cid}: {len(payload['patterns'])} pattern(s) from "
            f"{len(rejections)} rejection(s)"
        )
    return out


def get_rejection_patterns(repo: Repository, campaign_id: int) -> Optional[dict]:
    _ensure_column(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT rejection_patterns FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row or not row["rejection_patterns"]:
        return None
    try:
        return json.loads(row["rejection_patterns"])
    except Exception:
        return None


def render_for_prompt(repo: Repository, campaign_id: int) -> str:
    """Inject into Director / scorer prompts so the AI avoids these."""
    pat = get_rejection_patterns(repo, campaign_id)
    if not pat or not pat.get("patterns"):
        return ""
    lines = [
        f"Patterns Chris has REJECTED in past clips for this campaign "
        f"(n={pat.get('n_rejections', 0)}) — avoid these explicitly:"
    ]
    for p in pat["patterns"]:
        lines.append(f"- {p}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
def _campaigns_to_analyze(repo: Repository, only: Optional[int]) -> list[int]:
    with repo.conn() as c:
        if only:
            return [only]
        return [r["campaign_id"] for r in c.execute(
            "SELECT DISTINCT campaign_id FROM clips WHERE status='rejected'"
        ).fetchall()]


def _pull_rejections(repo: Repository, campaign_id: int) -> list[dict]:
    with repo.conn() as c:
        rows = c.execute(
            "SELECT transcript_excerpt, hook_text, caption_text, ai_score "
            "FROM clips WHERE campaign_id=? AND status='rejected' "
            "ORDER BY id DESC LIMIT 12",
            (campaign_id,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "transcript": (r["transcript_excerpt"] or "")[:300],
            "hook": (r["hook_text"] or "")[:120],
            "caption": (r["caption_text"] or "")[:300],
            "ai_score": r["ai_score"],
        })
    return out


def _ask_claude(rejections: list[dict]) -> Optional[dict]:
    if not settings.anthropic_api_key:
        return None
    try:
        from engine import llm_compat as anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    blob = json.dumps(rejections, indent=2)[:8000]
    prompt = _REJECT_PROMPT.replace("__CLIPS__", blob)
    try:
        resp = client.messages.create(
            model=settings.anthropic_model, max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[reject-learn] Claude call failed: {e}")
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    pats = obj.get("patterns") or []
    if not isinstance(pats, list):
        return None
    return {"patterns": [str(p)[:300] for p in pats if str(p).strip()][:6]}


_COL_CHECKED = False


def _ensure_column(repo: Repository) -> None:
    global _COL_CHECKED
    if _COL_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "rejection_patterns" not in cols:
            c.execute("ALTER TABLE campaigns ADD COLUMN rejection_patterns TEXT")
    _COL_CHECKED = True


def _persist(repo: Repository, campaign_id: int, payload: dict) -> None:
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET rejection_patterns = ? WHERE id = ?",
            (json.dumps(payload), campaign_id),
        )
