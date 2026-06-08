"""Auto-fill and submit a clip via the Whop Content Rewards submission form.

Picks a post for the campaign (newest 'posted' status by default), looks
up the campaign + URL, and runs the WhopSubmitter against the brief's
sub-campaign.

Usage:
    # Auto-pick the newest post for campaign 43:
    python scripts/submit_to_whop.py 43
    # Use a specific post:
    python scripts/submit_to_whop.py 43 --post 17
    # Specify sub-campaign by substring:
    python scripts/submit_to_whop.py 43 --sub "Open Tab"
    # Dry-run (fills the form, does NOT click submit):
    python scripts/submit_to_whop.py 43 --dry-run --headed
    # See the browser:
    python scripts/submit_to_whop.py 43 --headed

The script ALWAYS dumps HTML + screenshots into data/debug/submit/ so we
can iterate selectors if anything misses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from scanner.whop_login import WhopSession
from scanner.whop_submitter import WhopSubmitter, SubmissionInputs


def main() -> int:
    p = argparse.ArgumentParser(prog="submit_to_whop")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--post", type=int, help="Post DB id to submit (default: newest 'posted' for this campaign)")
    p.add_argument("--sub", type=str, default=None, help="Sub-campaign title substring (omit to take the only one)")
    p.add_argument("--title", type=str, default=None, help="Override submission title (default: caption first line)")
    p.add_argument("--demographics", type=str, default=None, help="Path to demographics screenshot to attach")
    p.add_argument("--dry-run", action="store_true", help="Fill the form but don't click final submit")
    p.add_argument("--headed", action="store_true", help="Show the browser (recommended on first run)")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id = ?", (args.campaign_id,)).fetchone()
    if not camp:
        print(f"campaign {args.campaign_id} not found")
        return 2

    post = _resolve_post(repo, args)
    if not post:
        print(f"no post found to submit for campaign #{args.campaign_id} — post a clip first")
        return 2

    title = args.title or _title_from_post(post)
    video_url = post["post_url"]
    demographics = Path(args.demographics) if args.demographics else None

    print(f"Campaign:  #{camp['id']}  {camp['title']}")
    print(f"Post:      #{post['id']}  ({post['platform']})  {video_url}")
    print(f"Title:     {title}")
    print(f"Sub:       {args.sub or '(first/only)'}")
    if demographics:
        print(f"Image:     {demographics}")

    inputs = SubmissionInputs(title=title, video_url=video_url, demographics_image=demographics)

    with WhopSession(headless=not args.headed) as session:
        submitter = WhopSubmitter(session.page, debug=True)
        result = submitter.submit(
            program_title=camp["title"],
            sub_campaign_title=args.sub,
            inputs=inputs,
            community_id=camp.get("community_id"),
            dry_run=args.dry_run,
        )

    print()
    if result.ok:
        print(f"✅ {result.message}")
        if not args.dry_run:
            repo.add_submission(post_id=post["id"], campaign_id=camp["id"], submitted_url=video_url)
            print("   submission row written")
        return 0
    print(f"❌ {result.message}")
    print("   see data/debug/submit/ for HTML + screenshots")
    return 1


def _resolve_post(repo: Repository, args) -> Optional[dict]:
    with repo.conn() as c:
        if args.post:
            row = c.execute(
                "SELECT p.*, cl.caption_text AS caption_text "
                "FROM posts p LEFT JOIN clips cl ON cl.id = p.clip_id "
                "WHERE p.id = ?",
                (args.post,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT p.*, cl.caption_text AS caption_text "
                "FROM posts p LEFT JOIN clips cl ON cl.id = p.clip_id "
                "WHERE cl.campaign_id = ? AND p.status='posted' AND p.post_url IS NOT NULL "
                "ORDER BY p.posted_at DESC LIMIT 1",
                (args.campaign_id,),
            ).fetchone()
    return dict(row) if row else None


def _title_from_post(post: dict) -> str:
    caption = (post.get("caption_text") or "").strip()
    if not caption:
        return "Submission"
    first_line = caption.split("\n", 1)[0].strip()
    return first_line[:120] or "Submission"


if __name__ == "__main__":
    sys.exit(main())
