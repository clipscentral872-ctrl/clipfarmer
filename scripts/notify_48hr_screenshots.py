"""Telegram ping when a post crosses the 48-hour mark.

Whop requires submitters to send the 48-hour analytics screenshots (views,
country demographics, engagement) to the campaign's Support Chat. This
script identifies posts that have just crossed the 48hr mark and we
haven't already pinged about, fetches a fresh stats snapshot, and sends
a Telegram message with everything Chris needs to paste into the support
chat.

Idempotent: each post is pinged at most once (tracked via
`posts.analytics_48hr_notified_at`).

Usage:
    python scripts/notify_48hr_screenshots.py            # check all due
    python scripts/notify_48hr_screenshots.py --post 17  # force one
    python scripts/notify_48hr_screenshots.py --dry-run  # show, don't send
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from db.repository import Repository
from engine.analytics_tracker import AnalyticsTracker
from publisher.telegram_gate import TelegramGate


WINDOW_HOURS = 48


def main() -> int:
    p = argparse.ArgumentParser(prog="notify_48hr_screenshots")
    p.add_argument("--post", type=int, help="Force-ping this post id (ignores 48h cutoff)")
    p.add_argument("--dry-run", action="store_true", help="Print the message, don't actually send")
    args = p.parse_args()

    repo = Repository()
    posts = _select_due_posts(repo, args)
    if not posts:
        print(f"No posts due for {WINDOW_HOURS}h notification.")
        return 0

    print(f"Pinging {len(posts)} post(s):")
    for r in posts:
        print(f"  post #{r['id']} {r['platform']:10s}  {r['post_url']}")

    tracker = AnalyticsTracker()
    gate = TelegramGate()
    sent = 0
    for row in posts:
        post = dict(row)
        rules = _parse_rules(post)
        # Honour each campaign's own analytics rule.
        if not args.post:
            if not _is_due(post, rules):
                continue
        snap = tracker.fetch_for_post(post)
        msg = _build_message(post, snap, repo, rules)
        a = rules.get("analytics") or {}
        # Only auto-render a PNG when the brief actually asks for a screenshot.
        png_path = None
        if a.get("required") and (a.get("format") or "screenshot") == "screenshot":
            png_path = _maybe_render_yt_analytics(post)
        if args.dry_run:
            print("\n----- DRY RUN -----")
            print(msg)
            if png_path:
                print(f"(would attach: {png_path})")
            print("-------------------\n")
            continue
        if not gate.enabled:
            print("Telegram not configured — message would be:")
            print(msg)
            continue
        if png_path and png_path.exists():
            _send_photo(gate, png_path, msg)
        else:
            gate.notify(msg)
        repo.set_post_field(post["id"], analytics_48hr_notified_at=_now())
        print(f"  ✅ pinged post #{post['id']}" + (" (with analytics PNG)" if png_path else ""))
        sent += 1


def _maybe_render_yt_analytics(post: dict):
    """For YouTube posts, fetch detailed analytics and render a PNG. Returns path or None."""
    if (post.get("platform") or "").lower() != "youtube":
        return None
    video_id = post.get("platform_post_id")
    if not video_id:
        return None
    try:
        from engine.youtube_analytics import fetch_for_video
        from engine.analytics_renderer import render_analytics_png
    except Exception as e:
        logger.warning(f"[48hr] could not import yt analytics: {e}")
        return None
    snap = fetch_for_video(video_id)
    if snap is None:
        logger.warning(f"[48hr] no analytics snapshot for {video_id}")
        return None
    out = Path("data/screenshots") / f"yt_analytics_{video_id}.png"
    try:
        render_analytics_png(
            snap,
            out_path=out,
            campaign_title=post.get("campaign_title"),
            post_url=post.get("post_url"),
            captured_at=_now(),
        )
        return out
    except Exception as e:
        logger.warning(f"[48hr] render failed: {e}")
        return None


def _send_photo(gate, photo_path: Path, caption: str) -> None:
    import requests
    url = f"https://api.telegram.org/bot{gate.bot_token}/sendPhoto"
    with photo_path.open("rb") as fh:
        r = requests.post(
            url,
            data={
                "chat_id": gate.chat_id,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
            files={"photo": (photo_path.name, fh, "image/png")},
            timeout=120,
        )
    if r.status_code >= 400:
        logger.warning(f"[48hr] sendPhoto failed {r.status_code}: {r.text[:200]}")
        # Fallback to text-only
        gate.notify(caption)

    print(f"\nSent {sent} notification(s).")
    return 0


def _select_due_posts(repo: Repository, args) -> list:
    with repo.conn() as c:
        if args.post:
            return c.execute(
                "SELECT p.*, c.title AS campaign_title, c.submission_url AS submission_url "
                "FROM posts p "
                "LEFT JOIN clips cl ON cl.id = p.clip_id "
                "LEFT JOIN campaigns c ON c.id = cl.campaign_id "
                "WHERE p.id = ?",
                (args.post,),
            ).fetchall()
        # We need ALL eligible posts regardless of timing; we'll filter per-campaign
        # below based on each campaign's structured_rules.analytics.due_after_hours.
        return c.execute(
            "SELECT p.*, c.title AS campaign_title, c.submission_url AS submission_url, "
            "c.structured_rules AS campaign_rules_json "
            "FROM posts p "
            "LEFT JOIN clips cl ON cl.id = p.clip_id "
            "LEFT JOIN campaigns c ON c.id = cl.campaign_id "
            "WHERE p.status='posted' "
            "AND (p.analytics_48hr_notified_at IS NULL OR p.analytics_48hr_notified_at = '')"
        ).fetchall()


def _parse_rules(post: dict) -> dict:
    raw = post.get("campaign_rules_json")
    if not raw:
        return {}
    try:
        import json as _json
        return _json.loads(raw) or {}
    except Exception:
        return {}


def _is_due(post: dict, rules: dict) -> bool:
    """Return True if it's time to send the analytics ping for this post."""
    a = rules.get("analytics") or {}
    if not a.get("required"):
        return False
    due_hours = a.get("due_after_hours") or WINDOW_HOURS
    try:
        due_hours = float(due_hours)
    except (TypeError, ValueError):
        due_hours = WINDOW_HOURS
    posted_at = post.get("posted_at") or ""
    if not posted_at:
        return False
    try:
        posted_dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except Exception:
        return False
    elapsed = (datetime.now(timezone.utc) - posted_dt).total_seconds() / 3600.0
    return elapsed >= due_hours


def _build_message(post: dict, snap, repo: Repository, rules: dict) -> str:
    """Per-campaign Whop-ready summary, shaped by structured_rules.analytics."""
    title = post.get("campaign_title") or "campaign"
    url = post.get("post_url") or ""
    platform = post.get("platform") or ""
    submission_url = post.get("submission_url") or ""
    posted_at = post.get("posted_at") or ""

    a = rules.get("analytics") or {}
    fmt = (a.get("format") or "screenshot").lower()
    delivery = (a.get("delivery_channel") or "").lower()
    delivery_url = a.get("delivery_url") or ""
    required_elements = a.get("required_elements") or []

    stats_block = "(no stats — platform API unavailable)"
    if snap is not None:
        stats_block = (
            f"  Views:    {snap.views:,}\n"
            f"  Likes:    {snap.likes:,}\n"
            f"  Comments: {snap.comments:,}\n"
            f"  Shares:   {snap.shares:,}\n"
            f"  Saves:    {snap.saves:,}"
        )

    if fmt == "screenrecording":
        action = (
            "📹 <b>This campaign needs a SCREEN RECORDING</b> — not a screenshot.\n"
            "Open the platform analytics for the post and record yourself navigating it live "
            "(view count, audience demographics, etc.)."
        )
    else:
        action = (
            "📸 <b>This campaign needs a screenshot.</b>\n"
            "I've attached an auto-rendered analytics card; you can also screenshot the "
            "platform's own analytics page if a reviewer prefers the native UI."
        )

    if delivery == "google_form":
        delivery_line = f"<b>Submit to (Google Form):</b> {_esc(delivery_url) or '(URL not in brief)'}\n"
    elif delivery == "support_chat":
        delivery_line = f"<b>Send to:</b> the campaign's Support Chat on Whop\n"
    elif delivery and delivery != "none":
        delivery_line = f"<b>Send to ({delivery}):</b> {_esc(delivery_url) or 'see brief'}\n"
    else:
        delivery_line = "<b>Send to:</b> campaign Support Chat on Whop (brief didn't specify — default)\n"

    must_show = ""
    if required_elements:
        must_show = "<b>Must show:</b> " + ", ".join(required_elements) + "\n"

    return (
        f"{action}\n\n"
        f"<b>Campaign:</b> {_esc(title)}\n"
        f"<b>Platform:</b> {platform}\n"
        f"<b>Posted:</b> {posted_at}\n"
        f"<b>Post URL:</b> <code>{_esc(url)}</code>\n"
        + (f"<b>Whop submit URL:</b> {_esc(submission_url)}\n" if submission_url else "")
        + delivery_line
        + must_show
        + f"\n<b>Current stats:</b>\n<pre>{stats_block}</pre>"
    )


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())
