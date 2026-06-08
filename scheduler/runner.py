"""Run one posting slot.

This is the unit of work the scheduler dispatches each "slot" time. It now
serves two distinct flows in priority order:

  1. WAREHOUSE PATH (preferred): if an approved warehouse clip is slated
     for this slot's time window, publish it without re-asking for
     approval — Chris already said /approve during the daily promote run.

  2. LIVE PATH (fallback): if the warehouse is empty for this slot,
     produce a fresh clip end-to-end and run it through the inline
     Telegram approval gate (legacy behavior).

If neither path produces anything, it's a no-op and we wait for the next slot.

This module is consumed by:
  - `scripts/run_one_slot.py`   (manual / cron firing)
  - `.github/workflows/post_slot.yml` (cron-driven on GitHub Actions)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from db.repository import Repository
from orchestrator import (
    _try_auto_submit,
    produce_clips_for_campaign,
    publish_clip_to_all_platforms,
)
from publisher import MultiPlatformPublisher, TelegramGate
from .profit_ranker import pick_next_campaign_by_ev


# Window around the slot time during which a warehouse clip is considered
# "for this slot".  Slot times are 2h apart, so a 90-min window catches
# anything scheduled close to now without bleeding into the next slot.
WAREHOUSE_SLOT_WINDOW_MIN = 90


def run_one_slot(
    *,
    repo: Optional[Repository] = None,
    campaign_id_override: Optional[int] = None,
    auto_submit: bool = True,
    sub_campaign_title: Optional[str] = None,
) -> bool:
    """Run a single posting slot. Returns True if a clip was produced + posted,
    False if there was nothing to do."""
    repo = repo or Repository()

    # 1. Try the warehouse first — only when no manual override is requested.
    if campaign_id_override is None:
        posted = _try_publish_from_warehouse(repo, auto_submit=auto_submit, sub_campaign_title=sub_campaign_title)
        if posted:
            return True

    # 2. Fall back to producing fresh on the slot.
    if campaign_id_override is not None:
        with repo.conn() as c:
            row = c.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id_override,)).fetchone()
        campaign = dict(row) if row else None
    else:
        campaign = pick_next_campaign_by_ev(repo)
    if not campaign:
        logger.info("[slot] no eligible campaign right now")
        return False

    source_path = (campaign.get("current_source_path") or "").strip()
    if not source_path or not Path(source_path).exists():
        # Try auto-finding a YouTube source for campaigns that allow open sourcing.
        from engine.source_finder import find_and_download_source, _parse_structured_rules
        must_match = (_parse_structured_rules(campaign).get("source_must_match") or [])
        if must_match:
            logger.warning(
                f"[slot] campaign #{campaign['id']} '{campaign['title']}' has no source "
                f"and source_must_match={must_match}; cannot auto-find. Skipping."
            )
            return False

        logger.info(f"[slot] campaign #{campaign['id']} has no source — auto-finding on YouTube")
        path = find_and_download_source(campaign)
        if not path or not path.exists():
            logger.warning(f"[slot] auto-find failed for campaign #{campaign['id']}")
            return False
        from db.repository import Repository as _Repo  # local alias for type hinting
        repo.set_campaign_current_source(campaign["id"], str(path))
        source_path = str(path)
        campaign["current_source_path"] = source_path

    logger.info(
        f"[slot] running campaign #{campaign['id']} '{campaign['title']}' "
        f"with source {source_path}"
    )

    clips = produce_clips_for_campaign(campaign, source_path, n_clips=1)
    if not clips:
        logger.warning(f"[slot] engine produced 0 clips for #{campaign['id']}")
        return False
    clip = clips[0]
    logger.info(f"[slot] produced clip {clip.final_path} (score={clip.moment.score})")

    publisher = MultiPlatformPublisher()
    gate = TelegramGate()
    try:
        post_ids = publish_clip_to_all_platforms(repo, publisher, campaign, clip, gate=gate)
    except Exception as e:
        logger.exception(f"[slot] publish failed: {e}")
        return False
    if not post_ids:
        logger.info("[slot] not approved or publish skipped — slot ends with no post")
        return False

    if auto_submit:
        _try_auto_submit(repo, campaign, post_ids, sub_campaign_title=sub_campaign_title, gate=gate)

    logger.info(f"[slot] done — posts {post_ids}")
    return True


def _try_publish_from_warehouse(
    repo: Repository,
    *,
    auto_submit: bool,
    sub_campaign_title: Optional[str],
) -> bool:
    """Look for an approved warehouse clip slated for this slot's window.
    Reconstruct it into a ProducedClip-shaped object so the orchestrator's
    publisher can consume it the same way as a freshly-produced clip — just
    skipping the inline approval gate via `pre_approved=True`."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=WAREHOUSE_SLOT_WINDOW_MIN)).isoformat()
    window_end = (now + timedelta(minutes=WAREHOUSE_SLOT_WINDOW_MIN)).isoformat()

    row = repo.warehouse_approved_clip_for_slot(window_start, window_end)
    if not row:
        return False

    clip_id = row["id"]
    final_path = row["final_clip_path"]
    if not final_path or not Path(final_path).exists():
        logger.warning(
            f"[slot/warehouse] clip #{clip_id} approved but final video missing at "
            f"{final_path!r} — falling back to live path"
        )
        return False

    with repo.conn() as c:
        camp_row = c.execute(
            "SELECT * FROM campaigns WHERE id = ?", (row["campaign_id"],)
        ).fetchone()
    if not camp_row:
        logger.warning(f"[slot/warehouse] clip #{clip_id} has no campaign row")
        return False
    campaign = dict(camp_row)

    clip = _reconstruct_produced_clip(row)
    logger.info(
        f"[slot/warehouse] publishing approved clip #{clip_id} "
        f"(campaign #{campaign['id']} '{campaign['title']}')"
    )

    publisher = MultiPlatformPublisher()
    gate = TelegramGate()
    try:
        post_ids = publish_clip_to_all_platforms(
            repo, publisher, campaign, clip, gate=gate, pre_approved=True,
        )
    except Exception as e:
        logger.exception(f"[slot/warehouse] publish failed: {e}")
        return False
    if not post_ids:
        logger.info("[slot/warehouse] publish returned no posts — slot ends")
        return False

    repo.set_clip_field(clip_id, warehouse_state="posted")
    if auto_submit:
        _try_auto_submit(repo, campaign, post_ids,
                         sub_campaign_title=sub_campaign_title, gate=gate)
    logger.info(f"[slot/warehouse] done — posts {post_ids}")
    return True


def _reconstruct_produced_clip(row):
    """Rebuild a ProducedClip from a DB clip row so the orchestrator's
    publisher pipeline can consume it without knowing it came from the
    warehouse."""
    from engine.pipeline import ProducedClip
    from engine.scorer import ScoredMoment

    hashtags = []
    if row["suggested_hashtags"]:
        try:
            hashtags = json.loads(row["suggested_hashtags"])
        except Exception:
            pass

    moment = ScoredMoment(
        start_sec=row["start_sec"],
        end_sec=row["end_sec"],
        duration_sec=row["duration_sec"],
        score=row["ai_score"] or 0.0,
        reason=row["ai_reason"] or "",
        transcript_excerpt=row["transcript_excerpt"] or "",
        hook_text=row["hook_text"] or "",
        caption_text=row["caption_text"] or "",
        hashtags=hashtags,
    )
    final_path = Path(row["final_clip_path"]) if row["final_clip_path"] else None
    raw_path = Path(row["raw_clip_path"]) if row["raw_clip_path"] else final_path
    clip = ProducedClip(
        moment=moment,
        raw_path=raw_path,
        vertical_path=final_path,    # we don't persist the intermediate vertical separately
        final_path=final_path,
    )
    # Tell the orchestrator's _ensure_clip_in_db to reuse this row instead of inserting.
    clip.db_id = row["id"]
    return clip
