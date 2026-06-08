"""Refresh view/engagement counts for posted clips and save snapshots.

Walks the `posts` table for rows with status='posted' and pulls fresh
stats from each platform's API. Each call appends a row to `analytics`
(post_id, captured_at, views, likes, comments, ...).

Usage:
    python scripts/track_analytics.py                # refresh every posted clip
    python scripts/track_analytics.py --post 17      # one post
    python scripts/track_analytics.py --platform youtube
    python scripts/track_analytics.py --hours 48     # only posts posted within last N hours
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# Force UTF-8 stdout so emoji in log messages don't crash on Windows cp1252.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

from loguru import logger

from db.repository import Repository
from engine.analytics_tracker import AnalyticsTracker


def main() -> int:
    p = argparse.ArgumentParser(prog="track_analytics")
    p.add_argument("--post", type=int, help="Refresh just this post id")
    p.add_argument("--platform", choices=["youtube", "instagram", "tiktok"], help="Only this platform")
    p.add_argument("--hours", type=int, default=None, help="Only posts posted within the last N hours")
    args = p.parse_args()

    repo = Repository()
    posts = _select_posts(repo, args)
    if not posts:
        print("No posts to track.")
        return 0
    print(f"Tracking {len(posts)} post(s):")
    for r in posts:
        print(f"  post #{r['id']} {r['platform']:10s}  {r['post_url']}")

    tracker = AnalyticsTracker()
    ok = 0
    skipped = 0
    failed = 0
    for row in posts:
        try:
            snap = tracker.fetch_for_post(dict(row))
            if snap is None:
                skipped += 1
                continue
            repo.record_analytics(row["id"], {
                "views": snap.views,
                "likes": snap.likes,
                "comments": snap.comments,
                "shares": snap.shares,
                "saves": snap.saves,
                "watch_time_sec": snap.watch_time_sec,
                "raw": snap.raw,
            })
            print(
                f"  ✅ post #{row['id']}: views={snap.views}  likes={snap.likes}  "
                f"comments={snap.comments}  shares={snap.shares}  saves={snap.saves}"
            )
            ok += 1
        except Exception as e:
            logger.exception(f"post #{row['id']} failed: {e}")
            failed += 1

    print(f"\nDone: {ok} ok, {skipped} skipped, {failed} failed")
    tracker.close()
    return 0 if failed == 0 else 1


def _select_posts(repo: Repository, args) -> list:
    with repo.conn() as c:
        sql = "SELECT * FROM posts WHERE status='posted' AND post_url IS NOT NULL AND post_url != ''"
        params: list = []
        if args.post:
            sql += " AND id = ?"
            params.append(args.post)
        if args.platform:
            sql += " AND platform = ?"
            params.append(args.platform)
        if args.hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat(timespec="seconds")
            sql += " AND posted_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY posted_at DESC"
        return c.execute(sql, params).fetchall()


if __name__ == "__main__":
    sys.exit(main())
