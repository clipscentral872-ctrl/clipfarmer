"""Pre-flight integration check.

Usage:
    python scripts/health_check.py            # run + Telegram on failure
    python scripts/health_check.py --notify   # Telegram even on full pass
    python scripts/health_check.py --quiet    # text only, never Telegram
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.health_check import (
    run_all_checks, render_telegram, notify_if_failures,
)


def main() -> int:
    p = argparse.ArgumentParser(prog="health_check")
    p.add_argument("--notify", action="store_true",
                   help="Telegram even on full pass")
    p.add_argument("--quiet", action="store_true",
                   help="Print only; never Telegram")
    args = p.parse_args()

    results = run_all_checks()
    n_fail = sum(1 for r in results if not r.ok)
    print(f"\n{'=' * 60}")
    for r in results:
        icon = "✅" if r.ok else "❌"
        print(f"{icon} {r.component:<32} {r.message}")
        if not r.ok and r.fix_hint:
            print(f"   → {r.fix_hint}")
    print(f"{'=' * 60}")
    print(f"Result: {len(results) - n_fail}/{len(results)} OK\n")

    if args.quiet:
        return 0 if n_fail == 0 else 1
    if args.notify or n_fail > 0:
        try:
            from publisher.telegram_gate import TelegramGate
            gate = TelegramGate()
            if gate.enabled:
                gate.notify(render_telegram(results))
        except Exception:
            pass
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
