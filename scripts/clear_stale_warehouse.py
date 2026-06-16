"""Clear warehouse clips whose scheduled posting slot has already passed.

When the cron is paused or the system goes idle for days, warehouse clips
accumulate with `scheduled_post_at` in the past — they will never publish.
This script un-shelves them: warehouse_state -> NULL, scheduled_post_at -> NULL.
The clip rows + files stay on disk; only the warehouse intent is cleared so
the next producer run can re-slot fresh content.

Run via workflow_dispatch on clear_stale_warehouse.yml.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
from loguru import logger
from db.repository import Repository


def main() -> int:
    repo = Repository()
    now_iso = datetime.now(timezone.utc).isoformat()
    with repo.conn() as c:
        rows = c.execute(
            "SELECT id, campaign_id, scheduled_post_at, warehouse_state "
            "FROM clips "
            "WHERE warehouse_state IS NOT NULL "
            "  AND scheduled_post_at IS NOT NULL "
            "  AND scheduled_post_at < ?",
            (now_iso,),
        ).fetchall()
        n = len(rows)
        logger.info(f"[clear-stale] found {n} stale warehouse clip(s)")
        for r in rows[:20]:
            logger.info(
                f"  clip #{r['id']} campaign #{r['campaign_id']} "
                f"scheduled {r['scheduled_post_at']} state={r['warehouse_state']}"
            )
        if n:
            c.execute(
                "UPDATE clips "
                "SET warehouse_state = NULL, scheduled_post_at = NULL "
                "WHERE warehouse_state IS NOT NULL "
                "  AND scheduled_post_at IS NOT NULL "
                "  AND scheduled_post_at < ?",
                (now_iso,),
            )
            logger.info(f"[clear-stale] cleared {n} clip(s)")
        else:
            logger.info("[clear-stale] nothing to clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
