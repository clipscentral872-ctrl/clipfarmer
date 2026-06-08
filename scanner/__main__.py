"""CLI entrypoint for a one-off Whop scan.

Usage:
    python -m scanner              # full scan, headless if session is cached
    python -m scanner --debug      # save HTML + screenshots to data/debug/
    python -m scanner --headed     # force a visible browser window
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from config import settings
from db import Repository
from db.migrations import init_db

from .campaign_scanner import CampaignScanner
from .source_extractor import SourceExtractor
from .whop_login import WhopSession


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scanner")
    parser.add_argument("--debug", action="store_true", help="Save HTML + screenshots to data/debug/")
    parser.add_argument("--headed", action="store_true", help="Force a visible browser window")
    parser.add_argument(
        "--communities",
        help="Override .env list. Comma-separated slugs.",
    )
    args = parser.parse_args(argv)

    logger.add(settings.logs_dir / "scanner.log", rotation="20 MB", retention=5)
    init_db()
    repo = Repository()

    slugs = (
        [s.strip() for s in args.communities.split(",") if s.strip()]
        if args.communities
        else settings.whop_communities
    )
    if not slugs:
        logger.error("No community slugs configured. Set WHOP_COMMUNITIES in .env or pass --communities.")
        return 2

    logger.info(f"[scan] starting scan of {len(slugs)} community/communities: {slugs}")

    headless = False if args.headed else None  # let WhopSession decide based on cache

    with WhopSession(headless=headless) as session:
        scanner = CampaignScanner(session, repo, debug=args.debug)
        campaign_ids = scanner.scan(slugs)
        logger.info(f"[scan] upserted {len(campaign_ids)} campaign(s)")
        # Source-video extraction is paused until we wire per-campaign
        # navigation. The campaign list page doesn't link to source videos
        # directly; we need to click into each campaign first.

    # Print a summary so the user can see what we discovered.
    with repo.conn() as c:
        campaigns = c.execute(
            "SELECT id, community_name, title, payout_per_1k_views, "
            "       budget_remaining_pct, viability_score, submission_url "
            "FROM campaigns ORDER BY viability_score DESC NULLS LAST, discovered_at DESC LIMIT 20"
        ).fetchall()
        sources = c.execute(
            "SELECT campaign_id, source_url FROM source_videos ORDER BY id DESC LIMIT 50"
        ).fetchall()

    print("\n=== Campaigns (ranked by viability) ===")
    if not campaigns:
        print("  (none discovered)")
    for row in campaigns:
        payout = f"${row['payout_per_1k_views']:.2f}/1k" if row["payout_per_1k_views"] else "payout=?"
        budget = f"{row['budget_remaining_pct']:.0f}% left" if row["budget_remaining_pct"] is not None else "budget=?"
        score = f"score={row['viability_score']:.0f}" if row["viability_score"] is not None else "score=?"
        print(f"  #{row['id']:>3}  {(row['community_name'] or '')[:22]:<22}  {payout:<12}  {budget:<12}  {score:<10}  {(row['title'] or '')[:50]}")
        print(f"        url: {row['submission_url']}")

    print("\n=== Source videos ===")
    if not sources:
        print("  (none discovered)")
    for row in sources:
        print(f"  campaign #{row['campaign_id']:>3}  {row['source_url']}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
