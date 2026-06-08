"""Continuous opportunity scanner — runs hourly on Vyro / ClipStake /
ClipAffiliates web marketplaces, ingests anything new, and Telegram-alerts
Chris on any campaign that:

  - Is new since the last scan, AND
  - Has CPM >= NEW_CAMPAIGN_MIN_CPM, AND
  - The Director's decision is GO or CONSIDER

That's the "🆕 New $X/k campaign — Director says GO" alert he wakes up to
between briefings.

Use lightly — these scrapes spin up Playwright sessions, so don't pump
them every minute. Hourly is a reasonable middle ground.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.rules_extractor import extract_rules, RulesExtractionError
from scanner.vyro_session import VyroSession
from scanner.clipstake_session import ClipStakeSession
from scanner.clipaffiliates_session import ClipAffiliatesSession


SESSIONS = {
    "vyro": VyroSession,
    "clipstake": ClipStakeSession,
    "clipaffiliates": ClipAffiliatesSession,
}

NEW_CAMPAIGN_MIN_CPM = 0.50

# Telegram once per platform per 6h so the user isn't spammed about a
# stale session on every cron tick.
_NOTIFY_COOLDOWN_HOURS = 6


def _maybe_notify_session_expired(platform: str) -> None:
    from config import settings as _settings
    from datetime import datetime, timezone, timedelta
    stamp_path = _settings.project_root / ".auth" / f"{platform}.session_expired_stamp"
    now = datetime.now(timezone.utc)
    if stamp_path.exists():
        try:
            last = datetime.fromisoformat(stamp_path.read_text(encoding="utf-8").strip())
            if (now - last) < timedelta(hours=_NOTIFY_COOLDOWN_HOURS):
                return
        except Exception:
            pass
    stamp_path.write_text(now.isoformat(), encoding="utf-8")
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if gate.enabled:
            gate.notify(
                f"<b>🔑 {platform.capitalize()} session expired</b>\n"
                f"Run when convenient:\n"
                f"<code>python scripts/scan_web_marketplaces.py --only {platform} --headed</code>\n"
                f"<i>(I won't ask again for {_NOTIFY_COOLDOWN_HOURS}h)</i>"
            )
    except Exception:
        pass


def main() -> int:
    p = argparse.ArgumentParser(prog="scan_for_opportunities")
    p.add_argument("--only", choices=list(SESSIONS.keys()), default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    args = p.parse_args()

    from config import settings as _settings
    shots_dir = _settings.project_root / "data" / "screenshots" / "opportunities"

    repo = Repository()
    targets = [args.only] if args.only else list(SESSIONS.keys())
    new_campaigns: list[dict] = []
    # Per-campaign screenshots (only taken for NEW campaigns, not Chris's existing ones)
    per_campaign_shots: dict[int, Path] = {}

    for plat in targets:
        cls = SESSIONS[plat]
        try:
            with cls(
                headless=not args.headed,
                # Background scheduler call: refuse to pop a headed Chrome
                # at Chris if the session expired. Skip silently instead.
                allow_interactive_login=args.headed,
            ) as s:
                campaigns = s.scrape_campaigns(limit=100)
                logger.info(f"[opp] {plat}: scraped {len(campaigns)} card(s)")
                # Ingest first to identify which are new.
                plat_new: list[tuple[int, dict]] = []
                for c in campaigns:
                    cid_new, was_new = _ingest_if_new(repo, plat, c)
                    if was_new:
                        plat_new.append((cid_new, c))
                # Take a focused popup screenshot per NEW campaign only.
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                for cid_new, c in plat_new:
                    title = (c.get("title") or "")[:80]
                    safe_title = "".join(ch if ch.isalnum() else "_" for ch in title)[:50]
                    shot_path = shots_dir / f"{plat}_{cid_new}_{safe_title}_{ts}.jpg"
                    try:
                        shot = s.screenshot_campaign_card(title, shot_path)
                        if shot:
                            per_campaign_shots[cid_new] = shot
                    except Exception as e:
                        logger.warning(f"[opp] screenshot for #{cid_new} failed: {e}")
                    new_campaigns.append({
                        "id": cid_new, "marketplace": plat,
                        "title": c.get("title"), "cpm": c.get("cpm_usd"),
                    })
        except Exception as e:
            from scanner.marketplace_session import SessionNeedsRefreshError
            if isinstance(e, SessionNeedsRefreshError):
                logger.info(f"[opp] {plat} session expired — skipping silently this cycle")
                _maybe_notify_session_expired(plat)
            else:
                logger.warning(f"[opp] {plat} session failed: {e}")
            continue

    if not new_campaigns:
        print("No new campaigns found.")
        return 0

    print(f"\n{len(new_campaigns)} new campaign(s) ingested:")
    for n in new_campaigns:
        print(f"  + #{n['id']} [{n['marketplace']}] {n['title']} (${n['cpm']}/k)")

    # Brief each via the Director, then notify on GO/CONSIDER.
    notify = []
    try:
        from engine.brain.director import brief_for_campaign
        for n in new_campaigns:
            try:
                brief = brief_for_campaign(repo, n["id"], force=True)
            except Exception as e:
                logger.warning(f"[opp] briefing #{n['id']} failed: {e}")
                continue
            if not brief:
                continue
            cpm = n["cpm"] or 0
            if cpm < NEW_CAMPAIGN_MIN_CPM and brief.get("decision") != "go":
                continue
            if brief.get("decision") in ("go", "consider"):
                notify.append((n, brief))
    except Exception as e:
        logger.warning(f"[opp] director skipped: {e}")

    if notify and not args.no_notify:
        _send_alerts(notify, per_campaign_shots)
    return 0


def _ingest_if_new(repo: Repository, marketplace: str, c: dict) -> tuple[int, bool]:
    """Insert if not seen, update last_seen_at if seen. Returns (id, was_new)."""
    title = (c.get("title") or "").strip()
    if not title:
        return 0, False
    wid = f"{marketplace}::{title}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    brief_text = c.get("raw_text") or title
    try:
        structured = extract_rules(brief_text, campaign_title=title)
    except RulesExtractionError:
        structured = {}
    with repo.conn() as conn:
        existing = conn.execute(
            "SELECT id FROM campaigns WHERE whop_campaign_id=?", (wid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE campaigns SET last_seen_at=? WHERE id=?",
                (now, existing["id"]),
            )
            return existing["id"], False
        conn.execute(
            "INSERT INTO campaigns ("
            "whop_campaign_id, community_id, community_name, title, "
            "marketplace, payout_per_1k_views, campaign_brief, structured_rules, "
            "status, discovered_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wid, marketplace, marketplace.capitalize(), title,
             marketplace, c.get("cpm_usd"), brief_text, json.dumps(structured),
             "active", now, now),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return new_id, True


def _send_alerts(notify: list[tuple[dict, dict]], per_campaign_shots: dict[int, Path]) -> None:
    """One Telegram message per new opportunity, each with its OWN popup
    screenshot showing the campaign's rules + CPM + source — not Chris's
    existing content."""
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if not gate.enabled:
            return
        for n, brief in notify:
            icon = {"go": "🟢", "consider": "🟡"}.get(brief.get("decision"), "•")
            cpm = n["cpm"] or 0
            ev = brief.get("predicted_value_per_clip", 0)
            caption = (
                f"<b>🆕 New on {n['marketplace'].capitalize()} — {icon} {brief.get('decision').upper()}</b>\n"
                f"#{n['id']} {n['title']}\n"
                f"<b>${cpm}/k</b> · Predicted <b>${ev:.2f}/clip</b>\n"
                f"<i>{brief.get('winning_angle', '')[:200]}</i>"
            )
            shot = per_campaign_shots.get(n["id"])
            if shot:
                gate.send_photo(shot, caption=caption)
            else:
                gate.notify(caption)
    except Exception as e:
        logger.warning(f"[opp] telegram alert failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
