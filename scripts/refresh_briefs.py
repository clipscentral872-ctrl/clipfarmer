"""Generate / refresh creative briefs for every active campaign.

The Director runs Claude with each campaign's full context (CPM,
budget, rules, competitor insights, our learnings) and produces a
JSON brief that the orchestrator + scorer + (future) producer follow.

Usage:
    python scripts/refresh_briefs.py            # all active campaigns
    python scripts/refresh_briefs.py --only 48
    python scripts/refresh_briefs.py --no-notify
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.director import refresh_briefs_and_notify, brief_for_campaign


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_briefs")
    p.add_argument("--only", type=int, default=None)
    p.add_argument("--no-notify", action="store_true")
    args = p.parse_args()

    if args.no_notify:
        repo = Repository()
        if args.only:
            b = brief_for_campaign(repo, args.only, force=True)
            print(f"#{args.only}: {b}")
        else:
            with repo.conn() as c:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM campaigns WHERE status='active' OR status IS NULL"
                ).fetchall()]
            for cid in ids:
                b = brief_for_campaign(repo, cid, force=True)
                if b:
                    print(f"#{cid}: {b.get('decision').upper()} — "
                          f"${b.get('predicted_value_per_clip', 0):.2f}/clip — "
                          f"{b.get('winning_angle', '')[:80]}")
        return 0

    out = refresh_briefs_and_notify(Repository(), only=args.only)
    print(f"Briefed {len(out)} campaign(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
