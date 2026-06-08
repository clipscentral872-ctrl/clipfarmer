"""Scrape Whop's Top Performing Videos for a campaign and save them.

Usage:
    python scripts/scrape_top_performers.py <campaign_db_id>
    python scripts/scrape_top_performers.py <campaign_db_id> --sub "EnhancedGames Streamer Clips"
    python scripts/scrape_top_performers.py <campaign_db_id> --headed

The current scanner stores PROGRAMS as 'campaigns' (e.g. "Enhanced Games"),
and each program has one or more sub-campaigns (e.g. "EnhancedGames
Streamer Clips"). Use --sub to pick a specific sub-campaign; without it,
we take the first one shown after clicking the program.

This always dumps HTML + screenshots to data/debug/scrape_top/ so we can
iterate the selectors if extraction misses anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from scanner.top_performers_scraper import TopPerformersScraper
from scanner.whop_login import WhopSession


def main() -> int:
    p = argparse.ArgumentParser(prog="scrape_top_performers")
    p.add_argument("campaign_id", type=int, help="Campaign DB id")
    p.add_argument("--sub", type=str, default=None, help="Sub-campaign title (substring match)")
    p.add_argument("--headed", action="store_true", help="Show the browser (useful for debugging)")
    p.add_argument("--dry-run", action="store_true", help="Print results, do not save to DB")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        row = c.execute(
            "SELECT id, title, top_performers FROM campaigns WHERE id = ?",
            (args.campaign_id,),
        ).fetchone()
    if not row:
        print(f"campaign {args.campaign_id} not found")
        return 2
    program_title = row["title"]
    logger.info(f"target: program #{row['id']} {program_title!r}, sub={args.sub!r}")

    headless = not args.headed
    with WhopSession(headless=headless) as session:
        scraper = TopPerformersScraper(session.page, debug=True)
        performers = scraper.scrape(program_title=program_title, sub_campaign_title=args.sub)

    if not performers:
        print("\nNo top performers extracted.")
        print("Check data/debug/scrape_top/ for the dumped HTML and screenshots.")
        print("Pass --sub '<exact sub-campaign name>' if there are multiple sub-campaigns.")
        return 1

    payload = [p.to_dict() for p in performers]
    print(f"\nFound {len(payload)} top performer(s):")
    print(json.dumps(payload, indent=2))

    if args.dry_run:
        print("\n(dry-run: not saving)")
        return 0

    repo.set_campaign_top_performers(args.campaign_id, payload)
    print(f"\nSaved to campaign #{args.campaign_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
