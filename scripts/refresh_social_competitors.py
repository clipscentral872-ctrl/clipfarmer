"""Refresh competitor top_performers from YT/TT/IG real-app search.

For each active campaign, searches the platforms for what's winning
on that campaign's topic, stores URLs/views/titles into
campaigns.top_performers. The downstream deep_competitor + Director
chain consumes that automatically.

Usage:
    python scripts/refresh_social_competitors.py
    python scripts/refresh_social_competitors.py --only 49
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.social_search import refresh_social_top_performers


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_social_competitors")
    p.add_argument("--only", type=int, default=None)
    args = p.parse_args()

    out = refresh_social_top_performers(Repository(), campaign_id=args.only)
    if not out:
        print("No campaigns refreshed (none active or no search results).")
        return 0
    print(f"\nRefreshed competitor top performers for {len(out)} campaign(s):")
    for cid, n in out.items():
        print(f"  #{cid}: {n} new performer(s) added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
