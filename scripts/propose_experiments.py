"""Brain proposes inventive experiments per campaign and Telegrams them.

Usage:
    python scripts/propose_experiments.py            # all active campaigns
    python scripts/propose_experiments.py --only 48
    python scripts/propose_experiments.py --no-notify
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.experimenter import refresh_and_notify


def main() -> int:
    p = argparse.ArgumentParser(prog="propose_experiments")
    p.add_argument("--only", type=int, default=None)
    p.add_argument("--no-notify", action="store_true",
                   help="Print only; don't Telegram.")
    args = p.parse_args()

    if args.no_notify:
        from engine.brain.experimenter import propose_experiments
        repo = Repository()
        if args.only:
            props = propose_experiments(repo, args.only)
            print(f"#{args.only}: {props}")
        else:
            with repo.conn() as c:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM campaigns WHERE status='active' OR status IS NULL"
                ).fetchall()]
            for cid in ids:
                props = propose_experiments(repo, cid)
                if props:
                    print(f"\n#{cid}:")
                    for p_ in props:
                        print(f"  • {p_.get('hypothesis')}")
                        print(f"    action: {p_.get('action')}")
                        print(f"    why:    {p_.get('why')}")
        return 0

    out = refresh_and_notify(Repository(), campaign_id=args.only)
    print(f"Proposed experiments for {len(out)} campaign(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
