"""Daily morning briefing — the proactive Brain message Chris reads
with coffee.

Aggregates four things into one Telegram message:

  1. Yesterday's posts + view counts + implied $ earned
  2. Today's posting plan (campaigns the EV ranker picked, predicted $)
  3. New opportunities discovered overnight (campaigns added by scanners,
     high-CPM Discord finds, fresh competitor patterns)
  4. Quality improvement suggestions (gaps between us and winners)

Idea is Chris wakes up to a clear "here's what's happening, here's what
to do today" message — no need to ask the system, it tells you first.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from scheduler.profit_ranker import (
    cpm_for, expected_views, score_campaign,
)
from scheduler.quota import DAILY_CLIP_QUOTA, daily_quota_for_campaign


def build_briefing(repo: Repository, hours_lookback: int = 24) -> str:
    """Render the Telegram-ready daily briefing HTML."""
    parts: list[str] = []
    parts.append("<b>☕ Good morning Chris — your Brain's daily briefing</b>")

    warehouse = _warehouse_status(repo)
    if warehouse:
        parts.append("\n" + warehouse)

    yest = _yesterday_summary(repo, hours_lookback)
    if yest:
        parts.append("\n" + yest)

    plan = _today_plan(repo)
    if plan:
        parts.append("\n" + plan)

    opps = _new_opportunities(repo)
    if opps:
        parts.append("\n" + opps)

    qual = _quality_suggestions(repo)
    if qual:
        parts.append("\n" + qual)

    return "\n".join(parts)


def _warehouse_status(repo: Repository) -> str:
    """One-line-per-day snapshot of the 3-day content buffer + a count of
    clips currently waiting on Chris's review.  Lives at the top of the
    briefing because it's the most actionable: if D+1 is short, the rest
    of the day's plan can be adjusted; if review queue > 0, Chris knows
    he's the bottleneck."""
    try:
        counts = repo.warehouse_counts_per_day(days_ahead=3)
        TARGET = 6
        with repo.conn() as c:
            pending = c.execute(
                "SELECT COUNT(*) AS n FROM clips WHERE warehouse_state='pending_review'"
            ).fetchone()["n"]
            approved = c.execute(
                "SELECT COUNT(*) AS n FROM clips WHERE warehouse_state='approved'"
            ).fetchone()["n"]
    except Exception as e:
        logger.warning(f"[briefing] warehouse status skipped: {e}")
        return ""

    def _bar(have: int, target: int = TARGET) -> str:
        filled = min(have, target)
        return ("█" * filled) + ("░" * max(0, target - filled))

    lines = [
        "<b>📦 Content warehouse (3-day buffer)</b>",
        f"  Tomorrow (D+1): <code>{_bar(counts.get(1, 0))}</code> {counts.get(1, 0)}/{TARGET}",
        f"  D+2:            <code>{_bar(counts.get(2, 0))}</code> {counts.get(2, 0)}/{TARGET}",
        f"  D+3:            <code>{_bar(counts.get(3, 0))}</code> {counts.get(3, 0)}/{TARGET}",
    ]
    if pending:
        lines.append(f"  <b>⏳ Awaiting your review: {pending}</b>")
    if approved:
        lines.append(f"  ✅ Approved, ready to post: {approved}")
    if all(counts.get(d, 0) == 0 for d in (1, 2, 3)):
        lines.append(
            "  <i>⚠️ Warehouse empty — producer hasn't been able to add clips yet. "
            "Often caused by expired YouTube OAuth blocking source auto-find.</i>"
        )
    return "\n".join(lines)


def send(repo: Repository) -> None:
    msg = build_briefing(repo)
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if gate.enabled:
            gate.notify(msg)
    except Exception as e:
        logger.warning(f"[briefing] telegram send failed: {e}")


# ----------------------------------------------------------------------
def _yesterday_summary(repo: Repository, hours: int) -> Optional[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with repo.conn() as c:
        rows = c.execute(
            "SELECT p.id, p.platform, p.posted_at, p.post_url, cl.campaign_id, "
            "(SELECT MAX(views) FROM analytics WHERE post_id = p.id) AS views "
            "FROM posts p JOIN clips cl ON cl.id = p.clip_id "
            "WHERE p.status='posted' AND p.posted_at >= ?",
            (cutoff,),
        ).fetchall()
    if not rows:
        return None
    total_views = sum(int(r["views"] or 0) for r in rows)
    n_posts = len(rows)
    by_campaign: dict[int, list[int]] = {}
    for r in rows:
        by_campaign.setdefault(r["campaign_id"], []).append(int(r["views"] or 0))

    implied = 0.0
    with repo.conn() as c:
        for cid, vs in by_campaign.items():
            row = c.execute("SELECT title, payout_per_1k_views FROM campaigns WHERE id=?", (cid,)).fetchone()
            cpm = row["payout_per_1k_views"] if row and row["payout_per_1k_views"] else 0.50
            implied += sum(vs) * cpm / 1000.0

    return (
        f"<b>📊 Last {hours}h</b>\n"
        f"  • {n_posts} post{'s' if n_posts != 1 else ''} across {len(by_campaign)} campaign(s)\n"
        f"  • <b>{total_views:,}</b> total views\n"
        f"  • <b>~${implied:.2f}</b> implied earned"
    )


def _today_plan(repo: Repository) -> Optional[str]:
    """List the campaigns the EV ranker would pick for today, with predicted $."""
    with repo.conn() as c:
        rows = c.execute(
            "SELECT * FROM campaigns "
            "WHERE (status IS NULL OR status='active')"
        ).fetchall()
    if not rows:
        return None
    plans: list[tuple[float, dict, dict, int]] = []
    for row in rows:
        camp = dict(row)
        quota = daily_quota_for_campaign(camp)
        if quota <= 0:
            continue
        s = score_campaign(repo, camp)
        plans.append((s["ev_usd"], camp, s, quota))
    if not plans:
        return None
    plans.sort(key=lambda t: t[0], reverse=True)
    lines = [f"<b>🎯 Today's plan ({len(plans)} eligible campaign(s))</b>"]
    total_predicted = 0.0
    for ev, camp, s, quota in plans[:6]:
        predicted = ev * quota
        total_predicted += predicted
        lines.append(
            f"  • #{camp['id']} {camp['title'][:34]:<34} ${ev:.2f}/clip × {quota} = <b>${predicted:.2f}</b>"
        )
    lines.append(f"  <i>Total predicted: ~${total_predicted:.2f}</i>")
    return "\n".join(lines)


def _new_opportunities(repo: Repository) -> Optional[str]:
    """Campaigns added in the last 24h + their Director decision."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with repo.conn() as c:
        rows = c.execute(
            "SELECT id, title, marketplace, payout_per_1k_views, creative_brief "
            "FROM campaigns WHERE discovered_at >= ? "
            "AND (status IS NULL OR status='active')",
            (cutoff,),
        ).fetchall()
    if not rows:
        return None
    lines = [f"<b>🆕 {len(rows)} new opportunity(ies) discovered</b>"]
    for r in rows[:8]:
        cpm = r["payout_per_1k_views"] or "?"
        brief_raw = r["creative_brief"]
        decision = "?"
        if brief_raw:
            try:
                brief = json.loads(brief_raw)
                decision = brief.get("decision", "?").upper()
            except Exception:
                pass
        icon = {"GO": "🟢", "CONSIDER": "🟡", "NO": "🔴"}.get(decision, "•")
        plat = (r["marketplace"] or "?").lower()
        lines.append(
            f"  {icon} #{r['id']} [{plat}] {r['title'][:40]:<40} ${cpm}/k  Director: {decision}"
        )
    return "\n".join(lines)


def _quality_suggestions(repo: Repository) -> Optional[str]:
    """Up to 2 dev-needed experiment suggestions — patterns competitors use
    that we can't currently execute."""
    with repo.conn() as c:
        rows = c.execute(
            "SELECT id, title, experiments FROM campaigns "
            "WHERE experiments IS NOT NULL AND (status IS NULL OR status='active')"
        ).fetchall()
    if not rows:
        return None
    dev_suggestions: list[tuple[str, str, str]] = []
    for r in rows:
        try:
            payload = json.loads(r["experiments"])
            props = payload.get("proposals") or []
        except Exception:
            continue
        for p in props:
            if not p.get("auto_testable") and p.get("hypothesis"):
                dev_suggestions.append((
                    r["title"][:30],
                    p["hypothesis"][:140],
                    p.get("needs_dev_for", "")[:200],
                ))
    if not dev_suggestions:
        return None
    lines = ["<b>🛠 Quality improvements I can't auto-test (your call to build)</b>"]
    for title, hyp, dev in dev_suggestions[:3]:
        lines.append(f"  • [{title}] {hyp}")
        if dev:
            lines.append(f"    <i>To build: {dev[:140]}</i>")
    return "\n".join(lines)
