"""Cross-campaign performance comparison + rebalancing proposals.

For each active campaign, compute:
  - views_per_clip      (median across this campaign's posted+tracked clips)
  - implied_earnings_per_clip = views_per_clip × cpm_usd / 1000
  - total_implied_earnings = sum of implied earnings over the window

Then rank campaigns by earnings_per_clip and suggest:
  - PROMOTE: top quartile → bump daily quota +1
  - KEEP:    middle → no change
  - DEMOTE:  bottom quartile → drop daily quota -1
  - PAUSE:   zero views or >7 days no clips → recommend pausing

The proposal is purely advisory for now — it's emitted as a Telegram
digest + saved to a `campaign_proposals` blob the bot can show on demand.
We do NOT auto-change quotas without sign-off, because rebalancing has
real Whop / Clipify submission implications Chris needs to vet.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from scheduler.profit_ranker import cpm_for


@dataclass
class CampaignPerformance:
    campaign_id: int
    title: str
    n_clips: int
    median_views: int
    total_views: int
    cpm_usd: float
    implied_earnings_per_clip: float
    implied_total_earnings: float
    last_post_at: Optional[str]


@dataclass
class Proposal:
    campaign_id: int
    title: str
    action: str           # 'promote' | 'keep' | 'demote' | 'pause'
    reason: str
    earnings_per_clip: float
    rank: int             # 1 = best


def compute_performance(
    repo: Repository,
    window_days: int = 14,
) -> list[CampaignPerformance]:
    """Per-campaign performance over the last `window_days`."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    perf: list[CampaignPerformance] = []
    with repo.conn() as c:
        campaigns = c.execute(
            "SELECT * FROM campaigns WHERE (status IS NULL OR status='active')"
        ).fetchall()
        for camp_row in campaigns:
            camp = dict(camp_row)
            views = []
            for r in c.execute(
                "SELECT MAX(a.views) AS v, MAX(p.posted_at) AS last_at "
                "FROM clips cl "
                "JOIN posts p ON p.clip_id = cl.id "
                "JOIN analytics a ON a.post_id = p.id "
                "WHERE cl.campaign_id = ? AND p.status='posted' "
                "AND (p.posted_at IS NULL OR p.posted_at >= ?) "
                "GROUP BY cl.id",
                (camp["id"], cutoff),
            ).fetchall():
                if r["v"]:
                    views.append(int(r["v"]))
            last_at_row = c.execute(
                "SELECT MAX(p.posted_at) AS last_at FROM clips cl "
                "JOIN posts p ON p.clip_id = cl.id "
                "WHERE cl.campaign_id = ? AND p.status='posted'",
                (camp["id"],),
            ).fetchone()
            last_at = last_at_row["last_at"] if last_at_row else None

            cpm = cpm_for(camp)
            median_v = int(statistics.median(views)) if views else 0
            total_v = sum(views)
            epc = median_v * cpm / 1000.0
            tot_earn = total_v * cpm / 1000.0
            perf.append(CampaignPerformance(
                campaign_id=camp["id"],
                title=camp["title"][:50],
                n_clips=len(views),
                median_views=median_v,
                total_views=total_v,
                cpm_usd=cpm,
                implied_earnings_per_clip=round(epc, 2),
                implied_total_earnings=round(tot_earn, 2),
                last_post_at=last_at,
            ))
    return perf


def propose_rebalance(
    perf: list[CampaignPerformance],
    *,
    pause_stale_days: int = 7,
    min_clips_for_demote: int = 3,
) -> list[Proposal]:
    """Produce one Proposal per campaign with a recommended action."""
    if not perf:
        return []
    # Sort by earnings-per-clip desc; rank #1 = top earner.
    sorted_perf = sorted(perf, key=lambda p: p.implied_earnings_per_clip, reverse=True)
    n = len(sorted_perf)
    proposals: list[Proposal] = []

    now = datetime.now(timezone.utc)
    for rank, p in enumerate(sorted_perf, start=1):
        # Pause candidates: zero views OR very stale
        action, reason = _decide(p, rank, n, now, pause_stale_days, min_clips_for_demote)
        proposals.append(Proposal(
            campaign_id=p.campaign_id,
            title=p.title,
            action=action,
            reason=reason,
            earnings_per_clip=p.implied_earnings_per_clip,
            rank=rank,
        ))
    return proposals


def _decide(p: CampaignPerformance, rank: int, n: int, now: datetime,
            stale_days: int, min_clips_demote: int) -> tuple[str, str]:
    # Pause: stale or zero performance
    if p.n_clips == 0:
        return "pause", "No posted clips in window — can't earn from this campaign."
    if p.last_post_at:
        try:
            last = datetime.fromisoformat(p.last_post_at.replace("Z", "+00:00"))
            if (now - last).days >= stale_days:
                return "pause", f"Last post was {(now - last).days} days ago — stale."
        except ValueError:
            pass
    if p.median_views == 0:
        return "pause", "Median views = 0 across the window."

    top_q = max(1, n // 4)
    bottom_q = max(1, n // 4)

    if rank <= top_q:
        return "promote", (
            f"Top earner: ${p.implied_earnings_per_clip:.2f}/clip "
            f"({p.median_views:,} median views × ${p.cpm_usd}/k). Consider +1 daily slot."
        )
    if rank > n - bottom_q and p.n_clips >= min_clips_demote:
        return "demote", (
            f"Bottom-quartile earner: only ${p.implied_earnings_per_clip:.2f}/clip "
            f"over {p.n_clips} clips. Consider -1 daily slot."
        )
    return "keep", (
        f"Mid-pack: ${p.implied_earnings_per_clip:.2f}/clip "
        f"({p.median_views:,} median views, n={p.n_clips})."
    )


def render_digest_html(perf: list[CampaignPerformance], proposals: list[Proposal]) -> str:
    if not proposals:
        return "<b>📊 Cross-campaign digest:</b> no data yet."
    lines = [f"<b>📊 Cross-campaign weekly digest</b>", ""]
    by_id = {p.campaign_id: p for p in perf}
    icons = {"promote": "📈", "keep": "✓", "demote": "📉", "pause": "⛔"}
    for prop in proposals:
        p = by_id[prop.campaign_id]
        icon = icons.get(prop.action, "•")
        lines.append(
            f"{icon} #{prop.rank} <b>{p.title}</b> — "
            f"${prop.earnings_per_clip:.2f}/clip "
            f"({p.median_views:,} med views, n={p.n_clips}) → <b>{prop.action.upper()}</b>"
        )
        lines.append(f"   <i>{prop.reason}</i>")
    return "\n".join(lines)


def refresh_proposals(repo: Repository, window_days: int = 14, notify: bool = True) -> dict:
    """Compute proposals, persist them, optionally Telegram-send the digest."""
    perf = compute_performance(repo, window_days=window_days)
    proposals = propose_rebalance(perf)
    _persist(repo, perf, proposals)
    if notify and proposals:
        try:
            from publisher.telegram_gate import TelegramGate
            gate = TelegramGate()
            if gate.enabled:
                gate.notify(render_digest_html(perf, proposals))
        except Exception as e:
            logger.warning(f"[cross-campaign] telegram digest send failed: {e}")
    return {
        "performance": [asdict(p) for p in perf],
        "proposals": [asdict(p) for p in proposals],
    }


_PROPOSALS_COLUMN_CHECKED = False


def _persist(repo: Repository, perf: list[CampaignPerformance], proposals: list[Proposal]) -> None:
    global _PROPOSALS_COLUMN_CHECKED
    if not _PROPOSALS_COLUMN_CHECKED:
        with repo.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
            if "proposal" not in cols:
                c.execute("ALTER TABLE campaigns ADD COLUMN proposal TEXT")
        _PROPOSALS_COLUMN_CHECKED = True
    by_id = {p.campaign_id: p for p in perf}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with repo.conn() as c:
        for prop in proposals:
            payload = {
                "action": prop.action,
                "reason": prop.reason,
                "rank": prop.rank,
                "earnings_per_clip": prop.earnings_per_clip,
                "n_clips": by_id[prop.campaign_id].n_clips,
                "median_views": by_id[prop.campaign_id].median_views,
                "computed_at": now,
            }
            c.execute(
                "UPDATE campaigns SET proposal = ? WHERE id = ?",
                (json.dumps(payload), prop.campaign_id),
            )
