"""Scan Sx Bot Clipify's #active-campaigns + per-streamer servers and
auto-add new campaigns to the DB.

Usage:
    python scripts/scan_clipify_directory.py            # scan + ingest
    python scripts/scan_clipify_directory.py --dry-run  # list only
    python scripts/scan_clipify_directory.py --headed   # show browser

Needs DISCORD_BURNER_EMAIL + DISCORD_BURNER_PASSWORD in .env. The burner
must have already joined the per-streamer servers it should ingest from —
this script tells you which ones it hasn't joined yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config import settings
from db.repository import Repository
from scanner.clipify_directory import ClipifyDirectoryScanner
from scanner.discord_session import DiscordSession


def main() -> int:
    p = argparse.ArgumentParser(prog="scan_clipify_directory")
    p.add_argument("--dry-run", action="store_true", help="List entries, don't ingest")
    p.add_argument("--headed", action="store_true", help="Show browser")
    args = p.parse_args()

    if not (settings.discord_burner_email and settings.discord_burner_password):
        print("DISCORD_BURNER_EMAIL / DISCORD_BURNER_PASSWORD missing from .env.")
        return 2

    repo = Repository()
    headless = not args.headed
    with DiscordSession(headless=headless) as ds:
        with ClipifyDirectoryScanner(session=ds) as scanner:
            entries = scanner.scan_directory()
            print(f"\nFound {len(entries)} active-campaigns entries:")
            for e in entries:
                inv = e.server_invite or "(no invite link in text)"
                print(f"  • {e.streamer_name}  → {inv}")

            if args.dry_run:
                return 0

            result = scanner.ingest_into_db(repo, entries)

    print(f"\nAdded   : {len(result['added'])} new clipify campaigns")
    for s in result["added"]:
        print(f"    + {s}")
    print(f"Updated : {len(result['updated'])} existing clipify campaigns")
    for s in result["updated"]:
        print(f"    ~ {s}")
    print(f"To join : {len(result['not_joined'])} streamers the burner isn't in yet")
    for e in result["not_joined"]:
        inv = e.server_invite or "(no invite link)"
        print(f"    ? {e.streamer_name}  → {inv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
