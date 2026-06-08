"""Scrape Vyro / ClipStake / ClipAffiliates marketplaces and ingest
every campaign into the DB as marketplace='<platform>'.

Usage:
    python scripts/scan_web_marketplaces.py                  # all 3
    python scripts/scan_web_marketplaces.py --only vyro      # one
    python scripts/scan_web_marketplaces.py --headed         # show browser
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
from scanner.clipstake_session import ClipStakeSession
from scanner.clipaffiliates_session import ClipAffiliatesSession


SESSIONS = {
    "vyro": VyroSession,
    "clipstake": ClipStakeSession,
    "clipaffiliates": ClipAffiliatesSession,
}


def main() -> int:
    p = argparse.ArgumentParser(prog="scan_web_marketplaces")
    p.add_argument("--only", choices=list(SESSIONS.keys()), default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--_internal_plat", default=None,
                   help="Internal: when set, run only this platform in-process (used by subprocess dispatch)")
    args = p.parse_args()

    # Multi-platform mode: dispatch each platform to its own Python subprocess
    # so Playwright's sync API doesn't choke on a second nested startup.
    if args._internal_plat is None and (args.only is None):
        import subprocess
        rc_total = 0
        for plat in SESSIONS:
            print(f"\n=== {plat.upper()} (subprocess) ===")
            child_args = [
                sys.executable, str(Path(__file__).resolve()),
                "--_internal_plat", plat,
            ]
            if args.headed:
                child_args.append("--headed")
            try:
                r = subprocess.run(child_args, timeout=60 * 12)
                rc_total += (r.returncode != 0)
            except subprocess.TimeoutExpired:
                print(f"  ! {plat} timed out — moving on")
                rc_total += 1
        return 1 if rc_total else 0

    # Single-platform mode (either --only or internal dispatch from above).
    plat = args._internal_plat or args.only
    if plat not in SESSIONS:
        print(f"unknown platform: {plat}")
        return 2
    cls = SESSIONS[plat]
    repo = Repository()
    print(f"\n=== {plat.upper()} ===")
    try:
        with cls(headless=not args.headed) as s:
            campaigns = s.scrape_campaigns(limit=100)
    except Exception as e:
        logger.exception(f"[{plat}] session failed: {e}")
        return 1
    print(f"  scraped {len(campaigns)} campaign(s)")
    added = 0
    for c in campaigns:
        cid = _ingest(repo, plat, c)
        if cid:
            added += 1
    print(f"  ingested {added} into DB")
    return 0


def _ingest(repo: Repository, marketplace: str, c: dict) -> int:
    title = (c.get("title") or "").strip()
    if not title:
        return 0
    brief_text = c.get("raw_text") or title
    try:
        structured = extract_rules(brief_text, campaign_title=title)
    except RulesExtractionError:
        structured = {}

    wid = f"{marketplace}::{title}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with repo.conn() as conn:
        existing = conn.execute(
            "SELECT id FROM campaigns WHERE whop_campaign_id=?", (wid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE campaigns SET title=?, marketplace=?, payout_per_1k_views=?, "
                "campaign_brief=?, structured_rules=?, last_seen_at=? WHERE id=?",
                (title, marketplace, c.get("cpm_usd"), brief_text,
                 json.dumps(structured), now, existing["id"]),
            )
            return existing["id"]
        conn.execute(
            "INSERT INTO campaigns ("
            "whop_campaign_id, community_id, community_name, title, "
            "marketplace, payout_per_1k_views, campaign_brief, structured_rules, "
            "status, discovered_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, marketplace, marketplace.capitalize(), title,
             marketplace, c.get("cpm_usd"), brief_text, json.dumps(structured),
             "active", now, now),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


if __name__ == "__main__":
    sys.exit(main())
