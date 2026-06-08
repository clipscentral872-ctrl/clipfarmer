"""Attribute outcomes back to specific Brain experiments + Telegram-summarize.

Run after analytics refresh + before next experiment proposal cycle so
yesterday's hits/misses inform tonight's bets.

Usage:
    python scripts/refresh_experiment_outcomes.py
    python scripts/refresh_experiment_outcomes.py --no-notify
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.experiment_outcomes import refresh_outcomes, refresh_and_notify


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_experiment_outcomes")
    p.add_argument("--no-notify", action="store_true")
    args = p.parse_args()

    if args.no_notify:
        out = refresh_outcomes(Repository())
    else:
        out = refresh_and_notify(Repository())

    print(f"\nAttributed {len(out)} experiment outcome(s).")
    for v in out:
        print(f"  {v['verdict']:>7} · #{v['campaign_id']} · {v['lift']:.2f}× · {v['views']:,} views "
              f"· {v['hypothesis'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
