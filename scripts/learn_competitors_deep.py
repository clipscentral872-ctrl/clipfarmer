"""Deep competitor learning — downloads + transcribes + extracts opener phrases.

Slow (Whisper). Run nightly or on-demand.

Usage:
    python scripts/learn_competitors_deep.py            # all campaigns with top_performers
    python scripts/learn_competitors_deep.py --only 48
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.deep_competitor import refresh_deep_insights


def main() -> int:
    p = argparse.ArgumentParser(prog="learn_competitors_deep")
    p.add_argument("--only", type=int, default=None)
    p.add_argument("--max-per-campaign", type=int, default=4)
    args = p.parse_args()

    result = refresh_deep_insights(
        Repository(),
        campaign_id=args.only,
        max_per_campaign=args.max_per_campaign,
    )
    if not result:
        print("Nothing learned. Need top_performers data first "
              "(run scripts/refresh_top_performers.py).")
        return 0
    for cid, d in result.items():
        print(f"#{cid}: openers={d.get('opener_phrases')}, "
              f"avg_cuts/sec={d.get('avg_cuts_per_sec')}, n={d.get('n_deep_analyzed')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
