"""Refiner: improve clips while they sit in the warehouse.

Per Chris's spec (2026-06-08): D+2 and D+3 clips should keep getting
better as the system learns.  This script runs every ~6h and does
additional QA / Editor passes on warehouse clips that:
  - are still > 24h from their scheduled post time (don't touch the next-up batch)
  - haven't already been refined N times

What "refine" means:
  - Re-score against the freshest brain weights / Director brief
  - Re-run QA layer (text + visual)
  - If QA suggests a fix, regenerate the caption / hook via Editor
  - Bump `refinement_count` and stamp `last_refined_at`

If a refinement pass produces a worse score, we keep the better version.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository


MAX_REFINEMENT_PASSES = 3
MIN_HOURS_UNTIL_POST = 24    # never refine within the 24h review window


def main() -> int:
    repo = Repository()
    clips = repo.warehouse_clips_for_refinement(
        max_passes=MAX_REFINEMENT_PASSES,
        min_hours_until_post=MIN_HOURS_UNTIL_POST,
    )
    if not clips:
        logger.info("[refine] no warehouse clips need refinement right now")
        return 0
    logger.info(f"[refine] {len(clips)} clip(s) eligible for refinement pass")

    # Imports deferred so this script is cheap to import even if the
    # brain modules are mid-refactor.
    try:
        from engine.brain.qa import run_qa_pass
        from engine.brain.editor import revise_clip_if_needed
    except ImportError as e:
        logger.warning(f"[refine] brain stack not importable: {e}; skipping run")
        return 0

    improved = 0
    skipped = 0
    for row in clips:
        clip_id = row["id"]
        try:
            qa = run_qa_pass(repo, clip_id)
        except Exception as e:
            logger.exception(f"[refine] QA crashed for clip #{clip_id}: {e}")
            skipped += 1
            continue

        if qa and qa.get("needs_revision"):
            try:
                changed = revise_clip_if_needed(repo, clip_id, qa)
                if changed:
                    improved += 1
                    logger.info(f"[refine] #{clip_id} revised: {qa.get('reason', '')[:120]}")
            except Exception as e:
                logger.exception(f"[refine] editor crashed for clip #{clip_id}: {e}")
                skipped += 1
                continue

        # Always bump the counter so we don't endlessly retry an unimprovable clip
        repo.mark_clip_refined(clip_id)

    logger.info(f"[refine] done — improved {improved}, skipped {skipped} (of {len(clips)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
