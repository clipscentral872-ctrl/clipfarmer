"""Add Vyro MrBeast campaigns (TT/YT + IG variants) directly to the DB
with the exact rules Chris pulled from the Vyro UI screenshots.

This bypasses the marketplace scraper for now — those campaigns are
already joined, we know the rules, just persist them.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository


# Source video for both: I Survived 7 Days in the Arctic (Frame.io)
SOURCE_URL = "https://f.io/RY-28ixg"

CAMPAIGNS = [
    {
        "vyro_id": "vyro::MrBeast (TT/YT) I Survived 7 Days in the Arctic",
        "title": "MrBeast (TT/YT) I Survived 7 Days in the Arctic",
        "platforms_required": ["tiktok", "youtube"],
        "ftc_disclosure_required": True,
    },
    {
        "vyro_id": "vyro::MrBeast (IG) I Survived 7 Days in the Arctic",
        "title": "MrBeast (IG) I Survived 7 Days in the Arctic",
        "platforms_required": ["instagram"],
        "ftc_disclosure_required": False,
    },
]

SHARED_RULES = {
    "required_hashtags": ["#mrbeast"],
    "forbidden_phrases": [
        "misrepresent",
        "paid ad",
        "engagement farming",
        "logos",
        "watermarks",
    ],
    "min_seconds": 15,
    "max_seconds": 60,
    "min_views_for_payout": 5000,
    "max_earnings_per_post": 1000,
    "client_approval_required": True,
    "source_links": [SOURCE_URL],
    "source_must_match": ["I Survived 7 Days in the Arctic", "MrBeast"],
}

CAMPAIGN_BRIEF = """Vyro MrBeast campaign — I Survived 7 Days in the Arctic

Source: https://f.io/RY-28ixg

REQUIRED in caption:
- #mrbeast (always)
- #paidpartner OR official paid-partnership label (TT/YT only — FTC disclosure)

CONTENT RULES:
- Do not edit footage in a way that misrepresents the video or MrBeast.
- Clips running as paid ads will be rejected.
- No engagement farming — videos must be genuinely entertaining.
- Do not use logos, hashtags, watermarks, or content not affiliated with this campaign.
- No botted views — flagged users are banned.
- Min video length: 15 seconds.
- Min views for payout: 5,000.
- Max earnings per post: $1,000.
- Each post is reviewed/approved by the client before counting.

PAYOUT: $1.00 per 1,000 views, capped at $1,000/post.
"""


def main() -> int:
    repo = Repository()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = updated = 0
    for c in CAMPAIGNS:
        structured = dict(SHARED_RULES)
        structured["platforms_required"] = c["platforms_required"]
        if c["ftc_disclosure_required"]:
            structured["required_hashtags"] = ["#mrbeast", "#paidpartner"]

        with repo.conn() as conn:
            existing = conn.execute(
                "SELECT id FROM campaigns WHERE whop_campaign_id = ?", (c["vyro_id"],)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE campaigns SET title=?, marketplace=?, "
                    "payout_per_1k_views=?, campaign_brief=?, structured_rules=?, "
                    "platforms_required=?, last_seen_at=? WHERE id=?",
                    (c["title"], "vyro", 1.00, CAMPAIGN_BRIEF,
                     json.dumps(structured), json.dumps(c["platforms_required"]),
                     now, existing["id"]),
                )
                cid = existing["id"]
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO campaigns ("
                    "whop_campaign_id, community_id, community_name, title, "
                    "marketplace, payout_per_1k_views, campaign_brief, structured_rules, "
                    "platforms_required, status, discovered_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["vyro_id"], "vyro", "Vyro", c["title"],
                     "vyro", 1.00, CAMPAIGN_BRIEF, json.dumps(structured),
                     json.dumps(c["platforms_required"]),
                     "active", now, now),
                )
                cid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                added += 1
        # Register the source video
        try:
            repo.add_source_video(
                campaign_id=cid, source_url=SOURCE_URL,
                title="I Survived 7 Days in the Arctic",
            )
        except Exception as e:
            print(f"  ! couldn't add source for #{cid}: {e}")
        action = "updated" if existing else "added"
        print(f"  ✅ {action}: campaign #{cid} {c['title']}")

    print(f"\nDone: {added} added, {updated} updated.")
    print("\nNext step: download the source video from Frame.io manually:")
    print(f"  {SOURCE_URL}")
    print("  Save to: C:\\Users\\chris\\clipfarmer\\data\\downloads\\mrbeast_arctic.mp4")
    print("Then run via Telegram: 'register source mrbeast_arctic for #<cid>'")
    print("Or: 'run a clip for #<cid>'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
