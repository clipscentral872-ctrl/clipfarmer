"""Backfill content_type on clips that don't have it yet.

Run once after the column is added, then style_classifier handles new
clips automatically at production time.

Usage:
    python scripts/backfill_content_types.py
    python scripts/backfill_content_types.py --only-campaign 44
    python scripts/backfill_content_types.py --force   # re-tag everything
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.style_classifier import classify_clip


def main() -> int:
    p = argparse.ArgumentParser(prog="backfill_content_types")
    p.add_argument("--only-campaign", type=int, default=None)
    p.add_argument("--force", action="store_true", help="Re-tag clips that already have a content_type")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        sql = "SELECT id, campaign_id, transcript_excerpt, hook_text, content_type FROM clips"
        wheres = []
        params: list = []
        if args.only_campaign:
            wheres.append("campaign_id = ?")
            params.append(args.only_campaign)
        if not args.force:
            wheres.append("(content_type IS NULL OR content_type = '')")
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        rows = c.execute(sql, params).fetchall()

    if not rows:
        print("Nothing to backfill.")
        return 0

    print(f"Tagging {len(rows)} clip(s)…")
    ok = 0
    for r in rows:
        tag = classify_clip(
            transcript_excerpt=r["transcript_excerpt"] or "",
            hook_text=r["hook_text"],
        )
        if not tag:
            print(f"  ⏭ clip #{r['id']}: skipped (no/short transcript or API miss)")
            continue
        with repo.conn() as c:
            c.execute(
                "UPDATE clips SET content_type=?, content_type_reason=? WHERE id=?",
                (tag["style"], tag["reason"], r["id"]),
            )
        print(f"  ✅ clip #{r['id']} (camp #{r['campaign_id']}): {tag['style']:<18} "
              f"— {tag['reason'][:80]}")
        ok += 1
    print(f"\nDone: {ok}/{len(rows)} tagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
