"""Producer: fill the 3-day content warehouse.

Runs every 6 hours.  Each run:
  1. Inspects warehouse counts per day (D+1, D+2, D+3).
  2. Targets 6 clips/day (3 campaigns × 2 clips/day per [[clipfarmer-daily-targets]]).
  3. Walks D+1, D+2, D+3 in order and produces clips for any day under target,
     stopping at a per-run cap to keep individual runs short.
  4. Each clip is tagged with a `scheduled_post_at` matching one of the 6
     daily slot times (US Eastern), so the post slot worker can pick the
     right approved clip from the warehouse later.

Status flow this script writes:
    new clip → warehouse_state='warehouse', scheduled_post_at=<iso>

Subsequent stages:
    refine_warehouse.py  : improves clips while they sit (D+2/D+3)
    promote_for_review.py: surfaces D+1 clips to Chris's Telegram at 15:00 SAST
    post_slot.py         : pulls approved clips from warehouse + publishes
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from scheduler.profit_ranker import pick_next_campaign_by_ev


WAREHOUSE_DAYS = 3
CLIPS_PER_DAY_TARGET = 6                    # 3 campaigns × 2 clips
PER_RUN_PRODUCE_CAP = 3                     # at most N clips per producer run
SLOT_TIMES_ET = ("10:00", "12:00", "15:00", "17:00", "20:00", "22:00")


def _next_unfilled_slots(repo: Repository, target_count_per_day: int) -> list[datetime]:
    """Return ISO-ordered list of slot datetimes (UTC) that still need a clip,
    walking D+1 → D+2 → D+3, capped at PER_RUN_PRODUCE_CAP."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    counts = repo.warehouse_counts_per_day(days_ahead=WAREHOUSE_DAYS)
    today_et = datetime.now(et).date()

    out: list[datetime] = []
    for offset in range(1, WAREHOUSE_DAYS + 1):
        have = counts.get(offset, 0)
        need = max(0, target_count_per_day - have)
        if need <= 0:
            continue
        day = today_et + timedelta(days=offset)
        for hhmm in SLOT_TIMES_ET[:need]:
            hh, mm = hhmm.split(":")
            slot = datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=et)
            out.append(slot.astimezone(timezone.utc))
            if len(out) >= PER_RUN_PRODUCE_CAP:
                return out
    return out


def main() -> int:
    repo = Repository()
    slots = _next_unfilled_slots(repo, CLIPS_PER_DAY_TARGET)
    if not slots:
        logger.info("[warehouse] all 3 days fully stocked — nothing to produce")
        return 0
    logger.info(f"[warehouse] producing for {len(slots)} slot(s): {[s.isoformat() for s in slots]}")

    # Avoid circular import: orchestrator pulls in publisher which pulls in
    # the quiet-hours module — fine, but import here so this script can be
    # used in environments where the publisher stack hasn't fully loaded.
    from orchestrator import produce_clips_for_campaign, _ensure_clip_in_db
    from engine.source_finder import find_and_download_source, _parse_structured_rules

    # Pull the eligible campaign pool ONCE and iterate.  pick_next_campaign_by_ev
    # is deterministic — calling it in a loop returns the same #1 every time.
    candidates = _rank_eligible_campaigns(repo)
    if not candidates:
        logger.warning("[warehouse] no eligible campaigns — stopping")
        return 0
    logger.info(f"[warehouse] {len(candidates)} eligible candidate(s) in EV order: " +
                ", ".join(f"#{c['id']}" for c in candidates[:5]))

    failed_ids: set[int] = set()
    produced = 0
    for slot_at in slots:
        campaign = _next_unfailed(candidates, failed_ids)
        if not campaign:
            logger.warning("[warehouse] every eligible campaign failed source resolution — stopping")
            break

        # Ensure a downloadable source exists (same flow as runner.run_one_slot)
        source_path = (campaign.get("current_source_path") or "").strip()
        if not source_path or not Path(source_path).exists():
            must_match = (_parse_structured_rules(campaign).get("source_must_match") or [])
            if must_match:
                logger.warning(
                    f"[warehouse] campaign #{campaign['id']} needs source matching {must_match} — skipping"
                )
                failed_ids.add(campaign["id"])
                continue
            p = find_and_download_source(campaign)
            if not p or not p.exists():
                logger.warning(f"[warehouse] auto-find source failed for #{campaign['id']} — excluding from this run")
                failed_ids.add(campaign["id"])
                continue
            repo.set_campaign_current_source(campaign["id"], str(p))
            source_path = str(p)

        try:
            clips = produce_clips_for_campaign(campaign, source_path, n_clips=1)
        except Exception as e:
            logger.exception(f"[warehouse] producer crashed on #{campaign['id']}: {e}")
            failed_ids.add(campaign["id"])
            continue
        if not clips:
            logger.info(f"[warehouse] engine produced 0 clips for #{campaign['id']}")
            failed_ids.add(campaign["id"])
            continue

        clip = clips[0]
        # Insert into DB explicitly — `produce_clips_for_campaign` returns
        # engine-side ProducedClip objects but doesn't write to the clips
        # table; the orchestrator does that just before publishing.  For
        # warehouse mode we own the insert so we get a clip_id to tag.
        try:
            clip_id = _ensure_clip_in_db(repo, campaign, clip)
        except Exception as e:
            logger.exception(f"[warehouse] failed to insert clip into DB: {e}")
            continue

        repo.mark_clip_warehoused(clip_id, scheduled_post_at=slot_at.isoformat())
        logger.info(
            f"[warehouse] +1 clip #{clip_id} for #{campaign['id']} '{campaign['title']}' "
            f"slot {slot_at.isoformat()}"
        )
        produced += 1

    logger.info(f"[warehouse] done — {produced} clip(s) added to warehouse")
    return 0


def _rank_eligible_campaigns(repo: Repository) -> list[dict]:
    """All active campaigns sorted by EV descending.  Filters on the same
    rules as pick_next_campaign_by_ev (budget %, quota, source-or-findable,
    SKIP_TIKTOK) but returns the FULL list so the warehouse can fall through
    to the next-best when one fails."""
    import os
    from config import settings
    from scheduler.profit_ranker import score_campaign, _campaign_requires_tiktok
    from scheduler.quota import daily_quota_for_campaign, daily_clip_count, _has_source_or_can_find

    floor = settings.min_budget_remaining_pct
    skip_tiktok = (os.environ.get("SKIP_TIKTOK", "").lower() in ("1", "true", "yes", "on"))
    with repo.conn() as c:
        rows = c.execute(
            "SELECT * FROM campaigns "
            "WHERE (status IS NULL OR status='active') "
            "AND (budget_remaining_pct IS NULL OR budget_remaining_pct >= ?)",
            (floor,),
        ).fetchall()
    eligible: list[tuple[float, dict]] = []
    for row in rows:
        camp = dict(row)
        quota = daily_quota_for_campaign(camp)
        if quota <= 0:
            continue
        if daily_clip_count(repo, camp["id"]) >= quota:
            continue
        if not _has_source_or_can_find(camp):
            continue
        if skip_tiktok and _campaign_requires_tiktok(camp):
            continue
        s = score_campaign(repo, camp)
        eligible.append((s["ev_usd"], camp))
    eligible.sort(key=lambda t: t[0], reverse=True)
    return [c for _ev, c in eligible]


def _next_unfailed(candidates: list[dict], failed_ids: set[int]) -> dict | None:
    for c in candidates:
        if c["id"] not in failed_ids:
            return c
    return None


if __name__ == "__main__":
    sys.exit(main())
