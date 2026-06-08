"""Try to auto-download a campaign's source footage.

Reads the campaign's saved `source_links` (populated by `auto_extract_briefs`)
and attempts each one in order until one succeeds.

Handlers:
  - Google Drive shared file → direct HTTP with cookie confirmation
  - WeTransfer link → Playwright clicks Download (uses the existing Whop session)
  - Direct .mp4 URL → plain HTTP stream

Usage:
    python scripts/download_source.py <campaign_id>
    python scripts/download_source.py <campaign_id> --url "https://..."   # override
    python scripts/download_source.py <campaign_id> --headed              # see browser
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.source_downloader import download_source
from scanner.whop_login import WhopSession


def main() -> int:
    p = argparse.ArgumentParser(prog="download_source")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--url", type=str, default=None, help="Override saved source_links with this URL")
    p.add_argument("--headed", action="store_true")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        row = c.execute(
            "SELECT id, title, source_links FROM campaigns WHERE id = ?",
            (args.campaign_id,),
        ).fetchone()
    if not row:
        print(f"campaign {args.campaign_id} not found")
        return 2

    if args.url:
        urls = [args.url]
    else:
        try:
            urls = json.loads(row["source_links"] or "[]")
        except Exception:
            urls = []
    if not urls:
        print("No source_links saved on this campaign. Run auto_extract_briefs first,")
        print("or pass --url with a direct link.")
        return 1

    print(f"Campaign #{row['id']}: {row['title']}")
    print(f"Trying {len(urls)} source link(s):")
    for u in urls:
        print(f"  - {u}")

    # WeTransfer + Drive both want a Playwright session in the same browser
    # context that's already logged in to Whop (cookies sometimes help and
    # we avoid re-opening Chrome).
    with WhopSession(headless=not args.headed) as session:
        for url in urls:
            print(f"\nAttempting: {url}")
            path = download_source(url, page=session.page)
            if path and path.exists():
                print(f"\n✅ Saved → {path}")
                repo.set_campaign_current_source(args.campaign_id, str(path))
                print("Saved as campaign's current_source_path — scheduler will use it automatically.")
                print("Or run manually with:")
                print(f'  python -m orchestrator --campaign {args.campaign_id} --source "{path}"')
                return 0

    print("\n❌ No handler could download any of the source links.")
    print("Download manually and pass the local path to the orchestrator instead.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
