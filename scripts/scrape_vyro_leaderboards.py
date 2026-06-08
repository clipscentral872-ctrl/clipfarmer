"""For every active Vyro campaign, scrape its Campaign Leaderboard and
merge results into `campaigns.top_performers` so the existing
deep_competitor + Director pipeline learns from the real winners.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from scanner.vyro_session import VyroSession
from scanner.vyro_leaderboard import VyroLeaderboardScraper


def main() -> int:
    p = argparse.ArgumentParser(prog="scrape_vyro_leaderboards")
    p.add_argument("--only", type=int, default=None,
                   help="Limit to one campaign id")
    p.add_argument("--headed", action="store_true")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        if args.only:
            rows = c.execute(
                "SELECT id, title, top_performers FROM campaigns "
                "WHERE id=? AND marketplace='vyro'",
                (args.only,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, title, top_performers FROM campaigns "
                "WHERE marketplace='vyro' AND (status IS NULL OR status='active')"
            ).fetchall()

    if not rows:
        print("No active Vyro campaigns to scrape.")
        return 0

    headless = not args.headed
    try:
        sess = VyroSession(headless=headless, allow_interactive_login=args.headed)
        sess.start()
    except Exception as e:
        from scanner.marketplace_session import SessionNeedsRefreshError
        if isinstance(e, SessionNeedsRefreshError):
            print("Vyro session expired — run scripts/scan_web_marketplaces.py --only vyro --headed when convenient")
            return 0
        raise
    with sess:
        with VyroLeaderboardScraper(session=sess) as scraper:
            for row in rows:
                title = row["title"] or ""
                # Use parenthetical tag if present (e.g. "(TT/YT)" or "(IG)"),
                # else first word, as the unique-on-page substring.
                import re
                m = re.search(r"\([^)]+\)", title)
                substr = m.group(0) if m else title.split()[0]
                print(f"\n#{row['id']} {title} (substr={substr!r})")
                try:
                    lb = scraper.scrape_leaderboard(substr)
                except Exception as e:
                    print(f"  ! error: {e}")
                    continue
                if not lb:
                    print("  (no leaderboard rows scraped)")
                    continue
                # Merge into top_performers (de-dup by handle).
                existing = []
                if row["top_performers"]:
                    try:
                        existing = json.loads(row["top_performers"])
                    except Exception:
                        existing = []
                seen_handles = {e.get("clipper_handle") for e in existing if isinstance(e, dict)}
                added = 0
                for entry in lb:
                    if entry.get("clipper_handle") and entry["clipper_handle"] in seen_handles:
                        continue
                    existing.append(entry)
                    added += 1
                repo.set_campaign_top_performers(row["id"], existing)
                print(f"  ✓ scraped {len(lb)} rows; {added} new merged into top_performers")
                # Snapshot the top 3 by views
                top = sorted(lb, key=lambda x: x.get("views") or 0, reverse=True)[:3]
                for t in top:
                    h = t.get("clipper_handle") or "?"
                    v = t.get("views") or 0
                    e = t.get("est_earnings") or 0
                    print(f"    {h} — {v:,} views, ${e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
