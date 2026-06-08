"""Capture a YouTube Studio Audience screenshot for one of our posts.

First run will pop a headed browser asking you to sign into YouTube Studio
as the channel that owns the video. After that the session is cached.

Usage:
    # By post id (uses platform_post_id stored in the DB):
    python scripts/capture_yt_studio.py --post 6
    # By video id directly:
    python scripts/capture_yt_studio.py --video FRf-Xj5SbVE
    # Grab all four analytics tabs as separate PNGs:
    python scripts/capture_yt_studio.py --post 6 --all-tabs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from scanner.youtube_studio import YouTubeStudioCapture


def main() -> int:
    p = argparse.ArgumentParser(prog="capture_yt_studio")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--post", type=int, help="Post DB id (looks up its platform_post_id)")
    g.add_argument("--video", type=str, help="YouTube video id directly")
    p.add_argument("--all-tabs", action="store_true", help="Capture Overview + Reach + Engagement + Audience")
    p.add_argument("--headed", action="store_true", help="Force headed browser (debug)")
    args = p.parse_args()

    video_id = args.video
    if args.post:
        repo = Repository()
        with repo.conn() as c:
            row = c.execute(
                "SELECT platform_post_id, post_url FROM posts WHERE id = ?", (args.post,),
            ).fetchone()
        if not row:
            print(f"post {args.post} not found")
            return 2
        video_id = row["platform_post_id"]
        if not video_id:
            print(f"post {args.post} has no platform_post_id")
            return 2
        print(f"post #{args.post} → video {video_id} ({row['post_url']})")

    headless = None
    if args.headed:
        headless = False

    with YouTubeStudioCapture(headless=headless) as cap:
        if args.all_tabs:
            paths = cap.screenshot_all_tabs(video_id)
            print(f"\nCaptured {len(paths)} tab(s):")
            for path in paths:
                print(f"  {path}")
            return 0 if paths else 1
        path = cap.screenshot_audience(video_id)
        if path:
            print(f"\n✅ Saved: {path}")
            return 0
        print("❌ Capture failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
