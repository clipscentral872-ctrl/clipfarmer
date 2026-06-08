"""Add Vyro Dhar Mann + HUGE Conversations as stub campaigns.

Rules will be filled in once Chris sends the details screenshots; for now
we use the basic info from the marketplace cards so they're visible to
the system and can be picked.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository


STUBS = [
    {
        "vyro_id": "vyro::Dhar Mann Studios — Dhar Mann Podcast With Jordan Matter",
        "title": "Dhar Mann Studios — What Happens Next - Podcast With Jordan Matter",
        "cpm": 1.00,
        "platforms_required": ["instagram", "youtube", "tiktok"],
        "min_seconds": 15,
        "max_seconds": 60,
        "notes": "Podcast format — Brain's Substack learnings should transfer",
    },
    {
        "vyro_id": "vyro::HUGE Conversations — Jony Ive Ferrari",
        "title": "HUGE Conversations — Jony Ive Shows Most Controversial Ferrari Ever",
        "cpm": 1.00,
        "platforms_required": ["instagram", "youtube", "tiktok"],
        "min_seconds": 15,
        "max_seconds": 60,
        "notes": "Tech / interview content — Jony Ive + Ferrari",
    },
]


def main() -> int:
    repo = Repository()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for stub in STUBS:
        structured = {
            "platforms_required": stub["platforms_required"],
            "min_seconds": stub["min_seconds"],
            "max_seconds": stub["max_seconds"],
            "min_views_for_payout": 5000,
            "max_earnings_per_post": 1000,
            "client_approval_required": True,
            "_stub": True,  # marker so we know rules are pending
        }
        with repo.conn() as c:
            existing = c.execute(
                "SELECT id FROM campaigns WHERE whop_campaign_id=?", (stub["vyro_id"],)
            ).fetchone()
            if existing:
                cid = existing["id"]
                c.execute(
                    "UPDATE campaigns SET title=?, marketplace=?, payout_per_1k_views=?, "
                    "structured_rules=?, status=?, last_seen_at=? WHERE id=?",
                    (stub["title"], "vyro", stub["cpm"], json.dumps(structured),
                     "active", now, cid),
                )
                action = "updated"
            else:
                c.execute(
                    "INSERT INTO campaigns ("
                    "whop_campaign_id, community_id, community_name, title, "
                    "marketplace, payout_per_1k_views, "
                    "structured_rules, platforms_required, "
                    "status, discovered_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (stub["vyro_id"], "vyro", "Vyro", stub["title"],
                     "vyro", stub["cpm"],
                     json.dumps(structured), json.dumps(stub["platforms_required"]),
                     "active", now, now),
                )
                cid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                action = "added"
        print(f"  {action}: #{cid} {stub['title']}")
        print(f"    {stub['notes']}")
        print(f"    ⚠️  Rules + source URL pending — send screenshots to complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
