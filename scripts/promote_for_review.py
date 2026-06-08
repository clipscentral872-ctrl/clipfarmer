"""Promoter: surface D+1 warehouse clips to Chris's Telegram for review.

Runs once a day at 15:00 SAST (top of Chris's active window — see
[[user-quiet-hours]]).  Picks every warehouse clip scheduled to post
within the next 24h that hasn't been reviewed yet, sends each one to
Telegram, and waits for /approve or /reject inside the same workflow run.

If Chris doesn't respond inside the timeout, the clip stays in
`pending_review` and the next promote run re-surfaces it.  No clip ever
posts without an explicit approval.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from publisher.telegram_gate import TelegramGate, ApprovalStatus


PER_RUN_REVIEW_CAP = 8       # safety cap; we'd expect ~6/day at steady state
APPROVAL_WAIT_MINUTES = 30   # how long to wait for Chris on each clip


def main() -> int:
    repo = Repository()
    due = repo.warehouse_clips_due_for_review(within_hours=24)
    if not due:
        logger.info("[promote] no D+1 clips need review right now")
        return 0
    logger.info(f"[promote] {len(due)} clip(s) due for review (cap={PER_RUN_REVIEW_CAP})")

    gate = TelegramGate()
    if not gate.enabled:
        logger.warning("[promote] Telegram gate disabled — nothing to do")
        return 0

    processed = 0
    for row in due[:PER_RUN_REVIEW_CAP]:
        clip_id = row["id"]
        campaign_id = row["campaign_id"]

        with repo.conn() as c:
            campaign = c.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        if not campaign:
            logger.warning(f"[promote] clip #{clip_id} has no campaign — skipping")
            continue

        final_path = row["final_clip_path"]
        if not final_path or not Path(final_path).exists():
            logger.warning(f"[promote] clip #{clip_id} final video missing at {final_path} — skipping")
            continue

        hashtags = []
        if row["suggested_hashtags"]:
            try:
                hashtags = json.loads(row["suggested_hashtags"])
            except Exception:
                pass

        platforms = []
        if campaign["platforms_required"]:
            try:
                platforms = json.loads(campaign["platforms_required"])
            except Exception:
                pass

        structured_rules = None
        if campaign["structured_rules"]:
            try:
                structured_rules = json.loads(campaign["structured_rules"])
            except Exception:
                pass

        try:
            token = gate.send_clip_for_approval(
                video_path=Path(final_path),
                campaign_title=campaign["title"],
                campaign_payout=campaign["payout_per_1k_views"],
                hook_text=row["hook_text"] or "",
                caption_text=row["caption_text"] or "",
                hashtags=hashtags,
                platforms=platforms,
                structured_rules=structured_rules,
            )
        except Exception as e:
            logger.exception(f"[promote] send failed for clip #{clip_id}: {e}")
            continue

        repo.mark_clip_review_sent(clip_id, token=token)
        logger.info(f"[promote] sent clip #{clip_id} for review (token={token})")

        verdict = gate.wait_for_verdict(token, timeout_minutes=APPROVAL_WAIT_MINUTES)
        if verdict.status == ApprovalStatus.APPROVED:
            repo.mark_clip_reviewed(clip_id, verdict="approved")
            logger.info(f"[promote] clip #{clip_id} APPROVED")
        elif verdict.status == ApprovalStatus.REJECTED:
            repo.mark_clip_reviewed(clip_id, verdict="rejected", note=verdict.note)
            logger.info(f"[promote] clip #{clip_id} REJECTED ({verdict.note!r})")
        else:
            # TIMED_OUT or DISABLED — leave in pending_review; next promote
            # run picks it up again so Chris never loses anything.
            logger.info(f"[promote] clip #{clip_id} no verdict ({verdict.status.value}) — will retry next run")
        processed += 1

    logger.info(f"[promote] done — processed {processed}/{len(due)} clip(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
