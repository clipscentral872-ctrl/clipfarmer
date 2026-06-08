"""Search the saved Whop HTML for campaign-related text and print the
surrounding DOM so we can figure out the card structure to scrape.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HTML = Path("data/debug/deep__whop.com_joined_clip-farm-official_exp_sKCcnfigfcLoSb_app.html")


def show_around(text: str, needle: str, ctx: int = 600, max_hits: int = 5) -> None:
    starts = [m.start() for m in re.finditer(re.escape(needle), text)]
    print(f"\n=== {needle!r}: {len(starts)} hits ===")
    for i, s in enumerate(starts[:max_hits]):
        a = max(0, s - ctx)
        b = min(len(text), s + ctx)
        print(f"\n--- hit {i+1} at {s} ---")
        print(text[a:b])


def main() -> int:
    if not HTML.exists():
        print(f"Missing: {HTML}")
        return 1
    raw = HTML.read_text(encoding="utf-8", errors="replace")
    print(f"HTML length: {len(raw):,}")

    # Show context around interesting strings.
    for needle in ("Substack", "Ashlee", "Podcast Clipping", "Clipping", "Campaigns"):
        show_around(raw, needle, ctx=400, max_hits=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
