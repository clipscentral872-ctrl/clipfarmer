"""Feed a YouTube source video to a campaign from Chris's laptop.

Background: as of mid-2025, YouTube blocks free-tier cloud IPs (GitHub
Actions, AWS, GCP, etc.) at the SABR + PO Token layer.  Every download
attempt fails.  Chris's residential IP, however, works fine with vanilla
yt-dlp — so the pragmatic split is:

    Chris's laptop  →  downloads YouTube source videos
    GitHub Actions  →  reuses the cached source to make many clips

Usage:
    python scripts/feed_source.py <campaign_id> <youtube_url>
    python scripts/feed_source.py        # interactive: prompts for any campaign
                                         # missing a source

After download the script:
    1. Saves the MP4 to data/downloads/
    2. Sets campaigns.current_source_path in the local SQLite DB
    3. Reminds Chris to run deploy/bootstrap_github.sh to push state up to
       the cloud (cloud workflows then pick up the new source).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.downloader import Downloader, DownloadError


def main() -> int:
    repo = Repository()
    args = sys.argv[1:]

    if len(args) >= 2 and args[0].isdigit():
        # Direct mode: feed <id> <url>
        campaign_id = int(args[0])
        url = args[1]
        return _feed_one(repo, campaign_id, url)

    # Interactive mode: walk campaigns missing a source
    missing = _campaigns_missing_source(repo)
    if not missing:
        print("✓ Every active campaign already has a source — nothing to feed.")
        return 0
    print(f"\n{len(missing)} campaign(s) need a source video:\n")
    for row in missing:
        print(f"  #{row['id']:>3}  {row['title']}")
    print()
    for row in missing:
        url = input(f"#{row['id']} {row['title']} — paste YouTube URL (Enter to skip): ").strip()
        if not url:
            print("  skipped")
            continue
        rc = _feed_one(repo, row["id"], url)
        if rc != 0:
            return rc
    print(
        "\n✓ Done.  Run this to push the new source(s) to the cloud:\n"
        "    & \"C:\\Program Files\\Git\\bin\\bash.exe\" deploy/bootstrap_github.sh"
    )
    return 0


def _campaigns_missing_source(repo: Repository) -> list:
    with repo.conn() as c:
        rows = c.execute(
            "SELECT id, title, current_source_path "
            "FROM campaigns "
            "WHERE (status IS NULL OR status='active') "
            "ORDER BY id DESC"
        ).fetchall()
    out = []
    for r in rows:
        p = (r["current_source_path"] or "").strip()
        if not p or not Path(p).exists():
            out.append(r)
    return out


def _feed_one(repo: Repository, campaign_id: int, url: str) -> int:
    with repo.conn() as c:
        row = c.execute(
            "SELECT id, title FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
    if not row:
        print(f"✗ Campaign #{campaign_id} not found.")
        return 1

    print(f"\n[{row['title']}] downloading source from {url} ...")
    try:
        path = Downloader().download(url)
    except DownloadError as e:
        print(f"✗ Download failed: {e}")
        return 1
    if not path or not path.exists():
        print("✗ Downloader returned no file.")
        return 1

    repo.set_campaign_current_source(campaign_id, str(path))
    print(f"✓ Saved {path}  ({path.stat().st_size:,} bytes)")
    print(f"  Campaign #{campaign_id} current_source_path updated in DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
