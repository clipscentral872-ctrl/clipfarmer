"""Detect newly-discovered learnings and Telegram-notify Chris.

Called by `scripts/refresh_learnings.py` after a refresh. We compare the
fresh learnings to whatever was previously persisted on the campaign
row (which we cache as a separate `learnings_prev` field) and surface:

  - NEW winners (a feature value crossed the lift threshold this run)
  - PROMOTED winners (lift increased significantly with more data)
  - LOST winners (was a winner last run, now isn't — pattern faded)
  - PLATFORM SHIFTS (a platform changed from "fine" to "drop" / "prioritize")

Each surface fires a Telegram message so Chris is told *why* the system
will behave differently from tomorrow onward.

Notifications are best-effort — failing to send doesn't block learning.
"""

from __future__ import annotations

import json
from typing import Optional

from loguru import logger

from db.repository import Repository


# How much lift must increase before we treat it as a "promotion".
PROMOTION_LIFT_DELTA = 0.3


def detect_and_notify(repo: Repository, campaign_id: int, fresh: dict) -> list[str]:
    """Compare `fresh` vs the previously-stored learnings; Telegram-notify
    on new findings. Returns the messages that were sent (for logging)."""
    prev = _load_prev(repo, campaign_id)
    title = _campaign_title(repo, campaign_id)

    messages: list[str] = []
    messages.extend(_diff_winners(prev, fresh, campaign_id, title))
    messages.extend(_diff_platforms(prev, fresh, campaign_id, title))

    if messages:
        _send_telegram(messages)
    _persist_prev(repo, campaign_id, fresh)
    return messages


def _diff_winners(prev: Optional[dict], fresh: dict, cid: int, title: str) -> list[str]:
    prev_winners = {(w["feature"], w["value"]): w for w in (prev or {}).get("winners", [])}
    fresh_winners = {(w["feature"], w["value"]): w for w in fresh.get("winners", [])}
    out: list[str] = []

    for key, w in fresh_winners.items():
        feat, val = key
        if key not in prev_winners:
            out.append(
                f"<b>🧠 New winner in #{cid} {title}:</b> "
                f"<code>{feat}={val}</code> — "
                f"<b>{w.get('lift', 1.0):.2f}×</b> baseline "
                f"({w.get('median', 0):,} views, n={w.get('n', 0)}). "
                f"Future clips will prefer this pattern."
            )
            continue
        old_lift = prev_winners[key].get("lift", 1.0)
        if (w.get("lift", 0) - old_lift) >= PROMOTION_LIFT_DELTA:
            out.append(
                f"<b>🧠 Pattern strengthened in #{cid} {title}:</b> "
                f"<code>{feat}={val}</code> went from "
                f"{old_lift:.2f}× → <b>{w.get('lift', 1.0):.2f}×</b> baseline "
                f"(n={w.get('n', 0)})."
            )

    for key in prev_winners:
        if key not in fresh_winners:
            feat, val = key
            out.append(
                f"<b>🧠 Pattern faded in #{cid} {title}:</b> "
                f"<code>{feat}={val}</code> no longer beats baseline. "
                f"System will stop favoring it."
            )

    return out


def _diff_platforms(prev: Optional[dict], fresh: dict, cid: int, title: str) -> list[str]:
    prev_recs = {p["platform"]: p for p in (prev or {}).get("platform_recommendations", [])}
    fresh_recs = {p["platform"]: p for p in fresh.get("platform_recommendations", [])}
    out: list[str] = []
    for plat, p in fresh_recs.items():
        old = prev_recs.get(plat)
        if not old or old.get("action") == p["action"]:
            continue
        verb = {"prioritize": "🔥 prioritize", "drop": "⛔ drop", "fine": "✓ neutral"}[p["action"]]
        out.append(
            f"<b>🧠 Platform shift in #{cid} {title}:</b> "
            f"{plat} → {verb} ({p['median_views']:,} median views over {p['n']} posts)."
        )
    return out


def _send_telegram(messages: list[str]) -> None:
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if not gate.enabled:
            return
        for m in messages:
            gate.notify(m)
    except Exception as e:
        logger.warning(f"[discoveries] telegram notify failed: {e}")


# ----------------------------------------------------------------------
def _load_prev(repo: Repository, campaign_id: int) -> Optional[dict]:
    _ensure_prev_column(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT learnings_prev FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row or not row["learnings_prev"]:
        return None
    try:
        return json.loads(row["learnings_prev"])
    except json.JSONDecodeError:
        return None


def _persist_prev(repo: Repository, campaign_id: int, learnings: dict) -> None:
    _ensure_prev_column(repo)
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET learnings_prev=? WHERE id=?",
            (json.dumps(learnings), campaign_id),
        )


def _campaign_title(repo: Repository, campaign_id: int) -> str:
    with repo.conn() as c:
        row = c.execute("SELECT title FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return (row["title"] if row else "(unknown)")[:40]


_PREV_COLUMN_CHECKED = False


def _ensure_prev_column(repo: Repository) -> None:
    global _PREV_COLUMN_CHECKED
    if _PREV_COLUMN_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "learnings_prev" not in cols:
            c.execute("ALTER TABLE campaigns ADD COLUMN learnings_prev TEXT")
    _PREV_COLUMN_CHECKED = True
