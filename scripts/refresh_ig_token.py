"""Refresh the Instagram Graph API token via Facebook's long-lived
exchange flow + re-derive the non-expiring Page token.

Run on demand or nightly via the scheduler.

Usage:
    python scripts/refresh_ig_token.py            # check + refresh if needed
    python scripts/refresh_ig_token.py --force    # refresh even if not due
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.ig_token_refresh import refresh, IGTokenError


def main() -> int:
    p = argparse.ArgumentParser(prog="refresh_ig_token")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    try:
        new_token, exp = refresh(force=args.force)
    except IGTokenError as e:
        print(f"❌ {e}")
        return 1
    head = new_token[:24] + "..." if new_token else "(missing)"
    print(f"✅ token: {head}")
    if exp is not None:
        if exp == 0:
            print(f"   never expires")
        else:
            days = exp / 86400.0
            print(f"   expires in ~{days:.1f} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
