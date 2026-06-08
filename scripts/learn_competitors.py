"""Learn from competitor top-performer clips on each campaign.

Reads `campaigns.top_performers` (populated by `refresh_top_performers.py`)
and runs each URL through metadata fetch + style classifier. Persists
aggregate insights as `campaigns.competitor_insights`, which the Brain
advisor injects into the scorer prompt.

Usage:
    python scripts/learn_competitors.py            # all campaigns
    python scripts/learn_competitors.py --only 48
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.competitor_learner import refresh_competitor_insights


def main() -> int:
    p = argparse.ArgumentParser(prog="learn_competitors")
    p.add_argument("--only", type=int, default=None)
    p.add_argument("--print", action="store_true")
    args = p.parse_args()

    result = refresh_competitor_insights(Repository(), campaign_id=args.only)
    if not result:
        print("No top-performer data yet. Run scripts/refresh_top_performers.py first.")
        return 0
    print(f"Learned from {len(result)} campaign(s):")
    for cid, insights in result.items():
        styles = ", ".join(
            f"{s['style']}({s['n']})" for s in insights.get("dominant_styles") or []
        )
        dur = insights.get("median_duration_sec")
        hooks = ", ".join(insights.get("common_hook_words") or [])
        print(f"  #{cid}: styles={styles or '?'}, ~{dur or '?'}s, hooks={hooks or '?'}")
        if args.print:
            print(json.dumps(insights, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
