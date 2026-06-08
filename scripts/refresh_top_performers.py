"""Refresh top-performer scrapes for every active marketplace='whop' campaign.

Scheduled daily by `python -m scheduler`. Feeds the profit ranker with
real competitor view counts so EV-based picking isn't blind.

Usage:
    python scripts/refresh_top_performers.py           # all active whop campaigns
    python scripts/refresh_top_performers.py --only 42 # one campaign
    python scripts/refresh_top_performers.py --headed  # debug

Skips campaigns scraped within the last `--max-age-hours` (default 22)
so a daily cron doesn't re-scrape if it just ran.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from scanner.top_performers_scraper import TopPerformersScraper
from scanner.whop_login import WhopSession


def _last_scraped(camp: dict) -> Optional[datetime]:
    """top_performers JSON has no timestamp inside — we use last_seen_at as
    a proxy since set_campaign_top_performers updates it."""
    raw = camp.get("last_seen_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_top_performers")
    p.add_argument("--only", type=int, default=None, help="Refresh just this campaign id")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--max-age-hours", type=float, default=22.0,
                   help="Skip if scraped within this many hours (default 22)")
    p.add_argument("--force", action="store_true", help="Ignore max-age, refresh everything")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        q = "SELECT * FROM campaigns WHERE (status IS NULL OR status='active')"
        params: list = []
        if args.only:
            q += " AND id = ?"
            params.append(args.only)
        else:
            # Only Whop campaigns — Clipify scraping uses a different code path.
            q += " AND (marketplace = 'whop' OR marketplace IS NULL)"
        rows = c.execute(q, params).fetchall()

    if not rows:
        logger.info("[refresh] no campaigns to refresh")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.max_age_hours)
    targets: list[dict] = []
    for row in rows:
        camp = dict(row)
        if not args.force and camp.get("top_performers"):
            last = _last_scraped(camp)
            if last and last >= cutoff:
                logger.info(f"[refresh] skip #{camp['id']} {camp['title']} — scraped {last.isoformat()}")
                continue
        targets.append(camp)

    if not targets:
        logger.info("[refresh] all campaigns are fresh; nothing to do")
        return 0

    logger.info(f"[refresh] refreshing {len(targets)} campaign(s)")

    headless = not args.headed
    refreshed = 0
    with WhopSession(headless=headless) as session:
        scraper = TopPerformersScraper(session.page, debug=True)
        for camp in targets:
            try:
                logger.info(f"[refresh] scraping #{camp['id']} {camp['title']}")
                performers = scraper.scrape(program_title=camp["title"])
                if not performers:
                    logger.warning(f"[refresh] #{camp['id']} returned 0 performers — left as-is")
                    continue
                payload = [p.to_dict() for p in performers]
                repo.set_campaign_top_performers(camp["id"], payload)
                refreshed += 1
                logger.info(f"[refresh] #{camp['id']} ← {len(payload)} performer(s)")
            except Exception as e:
                logger.exception(f"[refresh] #{camp['id']} failed: {e}")

    logger.info(f"[refresh] done: {refreshed}/{len(targets)} refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
