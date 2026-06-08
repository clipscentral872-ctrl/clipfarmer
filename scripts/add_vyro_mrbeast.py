"""Scrape Vyro marketplace, find Mr Beast campaign, add it to our DB
as marketplace='vyro' so the pipeline can clip + post for it.

Usage:
    python scripts/add_vyro_mrbeast.py            # auto-find + add
    python scripts/add_vyro_mrbeast.py --headed   # show browser

First run will be headed for login. After that the session is cached
at .auth/vyro.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.rules_extractor import extract_rules, RulesExtractionError
from scanner.vyro_session import VyroSession


def main() -> int:
    p = argparse.ArgumentParser(prog="add_vyro_mrbeast")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--name", default="Mr Beast", help="Substring of campaign title to find")
    args = p.parse_args()

    repo = Repository()
    headless = not args.headed

    with VyroSession(headless=headless) as v:
        campaign = v.find_campaign(args.name)
        if not campaign:
            print(f"Couldn't find a Vyro campaign matching {args.name!r}.")
            print("Run with --headed and inspect what's on the marketplace.")
            return 1

        detail = None
        if campaign.get("url"):
            detail = v.scrape_campaign_detail(campaign["url"])

    title = campaign["title"]
    brief_text = (detail.get("full_text") if detail else campaign.get("raw_text") or "")
    if not brief_text:
        brief_text = title
    try:
        structured = extract_rules(brief_text, campaign_title=title)
    except RulesExtractionError as e:
        logger.warning(f"rules extract failed: {e}")
        structured = {}

    # If detail page extracted hashtags / source links, merge them in.
    if detail:
        if detail.get("hashtags") and not structured.get("required_hashtags"):
            structured["required_hashtags"] = detail["hashtags"]
        if detail.get("mentions") and not structured.get("required_mentions"):
            structured["required_mentions"] = detail["mentions"]
        if detail.get("source_links"):
            structured.setdefault("source_links", detail["source_links"])

    vyro_id = f"vyro::{title}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with repo.conn() as c:
        existing = c.execute(
            "SELECT id FROM campaigns WHERE whop_campaign_id=?", (vyro_id,)
        ).fetchone()
        if existing:
            cid = existing["id"]
            c.execute(
                "UPDATE campaigns SET title=?, marketplace=?, payout_per_1k_views=?, "
                "campaign_brief=?, structured_rules=?, last_seen_at=? WHERE id=?",
                (title, "vyro", campaign.get("cpm_usd"),
                 brief_text, json.dumps(structured), now, cid),
            )
            action = "updated"
        else:
            c.execute(
                "INSERT INTO campaigns ("
                "whop_campaign_id, community_id, community_name, title, "
                "marketplace, payout_per_1k_views, campaign_brief, structured_rules, "
                "status, discovered_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vyro_id, "vyro", "Vyro", title, "vyro", campaign.get("cpm_usd"),
                 brief_text, json.dumps(structured),
                 "active", now, now),
            )
            cid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            action = "added"

    print(f"\n{action.upper()}: campaign #{cid} {title!r}")
    print(f"  CPM: ${campaign.get('cpm_usd') or '?'}")
    print(f"  URL: {campaign.get('url') or '?'}")
    if structured.get("source_links"):
        print(f"  Source links: {structured['source_links'][:3]}")
    if structured.get("required_hashtags"):
        print(f"  Hashtags: {structured['required_hashtags']}")
    if structured.get("required_mentions"):
        print(f"  Mentions: {structured['required_mentions']}")

    print("\nNext steps:")
    print("  1) Run scripts/scrape_top_performers.py", cid, "(after pasting top-performer URLs if Vyro has any)")
    print("  2) Use `find_source` bot tool or paste a Mr Beast video URL via Telegram")
    print("  3) Run `run a clip for", cid, "` in Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
