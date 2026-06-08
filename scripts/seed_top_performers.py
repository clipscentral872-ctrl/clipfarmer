"""Seed the top_performers JSON for a campaign by hand.

While the scraper isn't built yet, this lets you paste in what you see on
the Whop sub-campaign page (titles, view counts, platform, etc.) so the
scorer can immediately use that style signal.

Two ways to use it:

1. From a JSON file:
       python scripts/seed_top_performers.py <campaign_db_id> --file path/to/performers.json

2. Interactive (prompts you to paste a JSON list and press Ctrl-Z then Enter
   on Windows to finish):
       python scripts/seed_top_performers.py <campaign_db_id>

JSON shape (each item — all fields optional, but include at least title or hook):
    [
      {
        "title": "I tried Enhanced Games drugs for 30 days",
        "views": "847K",
        "est_earnings": 612,
        "platform": "tiktok",
        "length_sec": 47,
        "url": "https://www.tiktok.com/@user/video/...",
        "notes": "cold open with bottles on table, bold-claim hook"
      }
    ]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository


def main() -> int:
    p = argparse.ArgumentParser(prog="seed_top_performers")
    p.add_argument("campaign_id", type=int, help="Campaign DB id")
    p.add_argument("--file", type=str, help="Path to a JSON file with a list of performer dicts")
    p.add_argument("--show", action="store_true", help="Just print the current top_performers value and exit")
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

    print(f"Campaign #{row['id']}: {row['title']}")

    if args.show:
        print("\nCurrent top_performers:")
        print(row["top_performers"] or "(none)")
        return 0

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"file not found: {path}")
            return 2
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        print("\nPaste a JSON list of top-performer dicts.")
        print("On Windows, finish with Ctrl-Z then Enter.\n")
        raw = sys.stdin.read().strip()
        if not raw:
            print("nothing pasted; aborting.")
            return 1
        data = json.loads(raw)

    if not isinstance(data, list):
        print(f"expected a JSON list, got {type(data).__name__}")
        return 2

    repo.set_campaign_top_performers(args.campaign_id, data)
    print(f"\nSaved {len(data)} top performer(s) to campaign #{args.campaign_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
