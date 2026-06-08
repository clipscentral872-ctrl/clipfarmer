"""Walk Whop campaigns, fetch each brief automatically, extract structured rules.

This is the user-facing entry point for Layer 2: zero human input after
the first Whop login. For every campaign it:
  1. Drills into the Whop card to grab the on-page detail text.
  2. Follows any Google Docs links and pulls the brief verbatim.
  3. Feeds the concatenated text to Claude (`engine.rules_extractor`).
  4. Saves `campaign_brief` (raw) + `structured_rules` (JSON) on the row.

Usage:
    # Default — only campaigns missing campaign_brief:
    python scripts/auto_extract_briefs.py
    # One specific campaign:
    python scripts/auto_extract_briefs.py --id 43
    # Re-fetch all active campaigns (overrides existing briefs):
    python scripts/auto_extract_briefs.py --all --force
    # Just fetch text, don't run the rules extractor:
    python scripts/auto_extract_briefs.py --id 43 --skip-extract
    # See the browser while it works:
    python scripts/auto_extract_briefs.py --id 43 --headed
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.rules_extractor import extract_rules, RulesExtractionError
from scanner.brief_fetcher import BriefFetcher
from scanner.whop_login import WhopSession


def main() -> int:
    p = argparse.ArgumentParser(prog="auto_extract_briefs")
    p.add_argument("--id", type=int, help="Fetch only this campaign id")
    p.add_argument("--all", action="store_true", help="Process every active campaign")
    p.add_argument("--force", action="store_true", help="Re-fetch even if campaign already has a brief")
    p.add_argument("--skip-extract", action="store_true", help="Save raw brief but skip rules extraction")
    p.add_argument("--headed", action="store_true", help="Show the browser (debugging)")
    p.add_argument("--limit", type=int, default=None, help="Stop after N campaigns")
    args = p.parse_args()

    repo = Repository()
    targets = _select_targets(repo, args)
    if not targets:
        print("No campaigns to process.")
        return 0
    print(f"Targets: {len(targets)} campaign(s)")
    for t in targets:
        print(f"  #{t['id']}  {t['title']}")

    with WhopSession(headless=not args.headed) as session:
        fetcher = BriefFetcher(session.page, debug=True)

        ok = 0
        skipped = 0
        failed = 0
        for camp in targets:
            try:
                logger.info(f"=== campaign #{camp['id']}: {camp['title']} ===")
                bundle = fetcher.fetch(
                    program_title=camp["title"],
                    community_id=camp.get("community_id"),
                )
                if not bundle or not bundle.full_text.strip():
                    print(f"#{camp['id']}: no brief text found — see data/debug/fetch_brief/")
                    skipped += 1
                    continue

                if args.skip_extract:
                    repo.set_campaign_brief(camp["id"], bundle.full_text)
                    print(f"#{camp['id']}: saved brief ({len(bundle.full_text)} chars), skipped rules")
                    ok += 1
                    continue

                try:
                    rules = extract_rules(bundle.full_text, campaign_title=camp["title"])
                except RulesExtractionError as e:
                    print(f"#{camp['id']}: rules extraction failed: {e}")
                    # Still save the raw brief so a later run can retry.
                    repo.set_campaign_brief(camp["id"], bundle.full_text)
                    skipped += 1
                    continue

                repo.set_campaign_brief(camp["id"], bundle.full_text, rules)
                if bundle.source_links:
                    repo.set_campaign_source_links(camp["id"], bundle.source_links)
                src_count = len(bundle.source_links)
                doc_count = len(bundle.external_docs)
                print(
                    f"#{camp['id']}: ✅ brief ({len(bundle.full_text)} chars; "
                    f"{doc_count} doc{'s' if doc_count != 1 else ''}, "
                    f"{src_count} source link{'s' if src_count != 1 else ''}) "
                    f"+ structured rules saved"
                )
                ok += 1
                # Be polite to Whop — sleep between campaigns.
                time.sleep(2)
            except Exception as e:
                logger.exception(f"#{camp['id']} failed: {e}")
                failed += 1

    print(f"\nDone: {ok} ok, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 1


def _select_targets(repo: Repository, args) -> list[dict]:
    with repo.conn() as conn:
        if args.id:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (args.id,)).fetchone()
            return [dict(row)] if row else []
        if args.all:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status='active' "
                "ORDER BY viability_score DESC NULLS LAST"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status='active' "
                "AND (campaign_brief IS NULL OR campaign_brief = '') "
                "ORDER BY viability_score DESC NULLS LAST"
            ).fetchall()
        out = [dict(r) for r in rows]
        if not args.force and args.all:
            # When --all without --force, still skip campaigns that already have a brief.
            out = [c for c in out if not (c.get("campaign_brief") or "").strip()]
        if args.limit:
            out = out[: args.limit]
        return out


if __name__ == "__main__":
    sys.exit(main())
