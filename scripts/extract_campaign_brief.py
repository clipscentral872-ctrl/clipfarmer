"""Paste a campaign brief, Claude turns it into structured rules, save to DB.

Usage:
    python scripts/extract_campaign_brief.py <campaign_id> --file brief.txt
    python scripts/extract_campaign_brief.py <campaign_id>          # paste, Ctrl-Z + Enter to end
    python scripts/extract_campaign_brief.py <campaign_id> --show   # show what's saved
    python scripts/extract_campaign_brief.py <campaign_id> --clear  # wipe brief + rules
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.rules_extractor import extract_rules, RulesExtractionError


def main() -> int:
    p = argparse.ArgumentParser(prog="extract_campaign_brief")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--file", type=str, help="Path to a text/markdown file with the brief")
    p.add_argument("--show", action="store_true", help="Print current saved brief + rules and exit")
    p.add_argument("--clear", action="store_true", help="Wipe saved brief + structured_rules and exit")
    p.add_argument("--dry-run", action="store_true", help="Show extracted rules, do not save")
    args = p.parse_args()

    repo = Repository()
    with repo.conn() as c:
        row = c.execute(
            "SELECT id, title, campaign_brief, structured_rules FROM campaigns WHERE id = ?",
            (args.campaign_id,),
        ).fetchone()
    if not row:
        print(f"campaign {args.campaign_id} not found")
        return 2

    print(f"Campaign #{row['id']}: {row['title']}")

    if args.show:
        print("\nSaved brief:\n" + (row["campaign_brief"] or "(none)"))
        print("\nSaved structured_rules:\n" + (row["structured_rules"] or "(none)"))
        return 0

    if args.clear:
        with repo.conn() as c:
            c.execute(
                "UPDATE campaigns SET campaign_brief=NULL, structured_rules=NULL WHERE id=?",
                (args.campaign_id,),
            )
        print("Cleared brief + structured_rules.")
        return 0

    if args.file:
        brief_text = Path(args.file).read_text(encoding="utf-8")
    else:
        print("\nPaste the campaign brief (Ctrl-Z then Enter to finish on Windows):\n")
        brief_text = sys.stdin.read()

    brief_text = (brief_text or "").strip()
    if not brief_text:
        print("nothing to extract.")
        return 1

    try:
        rules = extract_rules(brief_text, campaign_title=row["title"])
    except RulesExtractionError as e:
        print(f"extraction failed: {e}")
        return 1

    print("\nExtracted structured rules:")
    print(json.dumps(rules, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("\n(dry-run: not saving)")
        return 0

    repo.set_campaign_brief(args.campaign_id, brief_text, rules)
    print(f"\nSaved brief + structured rules to campaign #{args.campaign_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
