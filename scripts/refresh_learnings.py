"""Refresh per-campaign learnings from the outcome data we've gathered.

Run nightly so the next morning's clips benefit from yesterday's data.

Usage:
    python scripts/refresh_learnings.py            # all campaigns
    python scripts/refresh_learnings.py --only 43  # one campaign
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain import refresh_learnings


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_learnings")
    p.add_argument("--only", type=int, default=None, help="Refresh one campaign id")
    p.add_argument("--print", action="store_true", help="Print the resulting JSON")
    args = p.parse_args()

    repo = Repository()
    result = refresh_learnings(repo, campaign_id=args.only)
    if not result:
        print("No learnings refreshed — not enough posted+tracked clips yet.")
        return 0
    print(f"Refreshed learnings for {len(result)} campaign(s):")
    for cid, learnings in result.items():
        winners = learnings.get("winners") or []
        baseline = learnings.get("baseline_median_views", 0)
        n = learnings.get("n_clips", 0)
        print(f"  #{cid}: {n} clips, baseline {baseline:,} views.")
        for w in winners[:4]:
            print(f"      ↑ {w['feature']}={w['value']}: {w['lift']:.2f}× "
                  f"({w['median']:,} views, n={w['n']})")
        if args.print:
            print(json.dumps(learnings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
