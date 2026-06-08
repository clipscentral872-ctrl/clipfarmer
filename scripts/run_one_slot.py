"""Run one posting slot — picks the next eligible campaign and produces+posts one clip.

This is the cron / Task-Scheduler entrypoint. Wire it to fire at the
slot times listed in `scheduler.SLOT_TIMES_LOCAL` (default: 09/11/13/15/17/19).

Usage:
    python scripts/run_one_slot.py                          # picker chooses campaign
    python scripts/run_one_slot.py --campaign 43            # force a specific campaign
    python scripts/run_one_slot.py --no-auto-submit         # don't run Whop submitter
    python scripts/run_one_slot.py --sub "Open Tab"         # sub-campaign for Whop submit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config import settings
from db.repository import Repository
from scheduler.runner import run_one_slot


def main() -> int:
    p = argparse.ArgumentParser(prog="run_one_slot")
    p.add_argument("--campaign", type=int, default=None, help="Force a specific campaign id")
    p.add_argument("--no-auto-submit", action="store_true", help="Skip Whop auto-submission")
    p.add_argument("--sub", type=str, default=None, help="Sub-campaign title substring for Whop submission")
    args = p.parse_args()

    logger.add(settings.logs_dir / "scheduler.log", rotation="20 MB", retention=10)

    repo = Repository()
    posted = run_one_slot(
        repo=repo,
        campaign_id_override=args.campaign,
        auto_submit=not args.no_auto_submit,
        sub_campaign_title=args.sub,
    )
    return 0 if posted else 1


if __name__ == "__main__":
    sys.exit(main())
