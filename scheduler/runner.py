"""Run one posting slot.

This is the unit of work the scheduler dispatches each "slot" time. It
does:
  1. Pick the next eligible campaign (under daily quota, has a source).
  2. Run the engine pipeline for ONE clip on that campaign.
  3. Push it through the Telegram approval gate.
  4. Publish the approved clip to all platforms.
  5. Optionally auto-submit to Whop.

If nothing's eligible it's a no-op and we wait for the next slot.

This module is consumed by:
  - `scripts/run_one_slot.py`   (manual / cron firing)
  - `scheduler/__main__.py`     (long-running APScheduler loop)
"""

from __future__ import annotations

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
