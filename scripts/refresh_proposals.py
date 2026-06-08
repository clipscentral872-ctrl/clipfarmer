"""Cross-campaign digest: per-campaign $/clip + promote/keep/demote/pause.

Run nightly. Sends a Telegram digest with the proposal list.

Usage:
    python scripts/refresh_proposals.py
    python scripts/refresh_proposals.py --window 7   # last 7 days
    python scripts/refresh_proposals.py --no-notify  # don't send Telegram
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain import refresh_proposals


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_proposals")
    p.add_argument("--window", type=int, default=14)
    p.add_argument("--no-notify", action="store_true")
    args = p.parse_args()

    result = refresh_proposals(
        Repository(),
        window_days=args.window,
        notify=not args.no_notify,
    )
    print(f"\nCampaigns evaluated: {len(result['performance'])}")
    for prop in result["proposals"]:
        print(f"  #{prop['rank']:>2} {prop['title'][:40]:<40} ${prop['earnings_per_clip']:>6.2f}/clip "
              f"→ {prop['action'].upper()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
