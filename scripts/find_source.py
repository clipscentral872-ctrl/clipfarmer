"""Find (and optionally download) a fresh YouTube source for a campaign.

For campaigns whose brief allows open sourcing (no `source_must_match`
restriction), this asks Claude to build a search query, runs yt-dlp,
ranks the candidates, and prints/downloads the winner.

Usage:
    # Search-only — print top candidate, do not download:
    python scripts/find_source.py 44
    # Search AND download — saves to data/downloads/ and updates
    # campaigns.current_source_path so the scheduler can use it next slot:
    python scripts/find_source.py 44 --download
    # See all candidates:
    python scripts/find_source.py 44 --candidates 15 --list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.source_finder import find_source, find_and_download_source, _search_youtube, _build_search_query, _parse_structured_rules


def main() -> int:
    p = argparse.ArgumentParser(prog="find_source")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--download", action="store_true", help="Download the picked source and save its path on the campaign row")
    p.add_argument("--list", action="store_true", help="Print all candidates before/instead of picking")
    p.add_argument("--candidates", type=int, default=10, help="How many YouTube results to consider")
    p.add_argument("--min-duration", type=int, default=180, help="Drop candidates shorter than this many seconds")
    p.add_argument("--query", type=str, default=None, help="Override the Claude-built search query")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        row = c.execute("SELECT * FROM campaigns WHERE id = ?", (args.campaign_id,)).fetchone()
    if not row:
        print(f"campaign {args.campaign_id} not found")
        return 2
    campaign = dict(row)
    structured = _parse_structured_rules(campaign)

    must_match = structured.get("source_must_match") or []
    if must_match:
        print(f"⚠️  Campaign #{campaign['id']} restricts sources to: {must_match}")
        print("    Auto-finding will probably violate the brief. Aborting.")
        return 1

    if args.list:
        query = args.query or _build_search_query(campaign, structured)
        print(f"query: {query!r}")
        candidates = _search_youtube(query, args.candidates)
        for i, c in enumerate(candidates):
            print(f"  [{i}] {c.short_blurb()}")
            print(f"        {c.url}")
        if not args.download:
            return 0

    pick = find_source(campaign, n_candidates=args.candidates, min_duration_sec=args.min_duration)
    if not pick:
        print("No usable candidate found.")
        return 1

    print(f"\n✅ Picked: {pick.url}")
    print(f"   {pick.short_blurb()}")

    if not args.download:
        print("\n(Pass --download to actually fetch it and save as the campaign's current_source_path.)")
        return 0

    print("\nDownloading...")
    path = find_and_download_source(campaign)
    if not path:
        print("Download failed.")
        return 1
    repo.set_campaign_current_source(campaign["id"], str(path))
    print(f"\n💾 Saved → {path}")
    print(f"Campaign #{campaign['id']} current_source_path updated.")
    print("Next: scheduler will use it on the next slot, or run manually:")
    print(f'  python -m orchestrator --campaign {campaign["id"]} --source "{path}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
