"""Tools the chat agent exposes to Claude.

Each tool is a (spec, implementation) pair:
  - `spec` is the JSON-schema-style description Claude receives when
    deciding what to do
  - `impl` is a Python callable that returns a string (Telegram-friendly)

All tools wrap functions already proven in the rest of the codebase —
the agent is the dispatcher, not a parallel implementation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from config import settings
from db.repository import Repository


# ----------------------------------------------------------------------
# Single pipeline runner — the bot can only run ONE clip pipeline at a time.
# Without this guard, two concurrent "run next clip" requests would spawn
# two orchestrators, both polling Telegram for /approve, fighting each
# other. The lock makes the second request fail fast with a clear message.
# ----------------------------------------------------------------------
_PIPELINE_LOCK = threading.Lock()
_PIPELINE_THREAD: Optional[threading.Thread] = None


def _is_pipeline_running() -> bool:
    return _PIPELINE_THREAD is not None and _PIPELINE_THREAD.is_alive()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


# ----------------------------------------------------------------------
# Public registry
# ----------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = []
IMPLS: dict[str, Callable] = {}


def _register(spec: dict, impl: Callable) -> None:
    TOOLS.append(spec)
    IMPLS[spec["name"]] = impl


def dispatch(name: str, args: dict) -> str:
    fn = IMPLS.get(name)
    if not fn:
        return f"(unknown tool: {name})"
    try:
        return fn(**(args or {}))
    except Exception as e:
        logger.exception(f"[bot] tool {name} crashed: {e}")
        return f"⚠️ Tool {name} crashed: {e}"


# ----------------------------------------------------------------------
# DB helpers shared by multiple tools
# ----------------------------------------------------------------------
def _repo() -> Repository:
    return Repository()


def _q(sql: str, params: tuple = ()) -> list[dict]:
    with _repo().conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ----------------------------------------------------------------------
# Tool: list_campaigns
# ----------------------------------------------------------------------
def list_campaigns_impl(only_active: bool = True, limit: int = 20) -> str:
    sql = "SELECT id, title, payout_per_1k_views, budget_remaining_pct, viability_score, " \
          "tracking_code, current_source_path " \
          "FROM campaigns "
    if only_active:
        sql += "WHERE (status IS NULL OR status='active') "
    sql += "ORDER BY viability_score DESC NULLS LAST LIMIT ?"
    rows = _q(sql, (limit,))
    if not rows:
        return "No campaigns in DB."
    lines = ["<b>Campaigns:</b>"]
    for r in rows:
        cpm = f"${r['payout_per_1k_views']:.2f}/1k" if r['payout_per_1k_views'] else "?"
        budget = f"{r['budget_remaining_pct']:.0f}%" if r['budget_remaining_pct'] is not None else "?"
        score = f"{r['viability_score']:.0f}" if r['viability_score'] else "?"
        flags = []
        if r.get("tracking_code"):
            flags.append("code ✓")
        if r.get("current_source_path"):
            flags.append("source ✓")
        flag = (" " + " · ".join(flags)) if flags else ""
        lines.append(f"  #{r['id']:>2} {r['title']} — {cpm}, budget {budget}, score {score}{flag}")
    return "\n".join(lines)


_register(
    {
        "name": "list_campaigns",
        "description": "List clipping campaigns currently in the DB. Use this when "
                       "the user asks 'what campaigns', 'show me campaigns', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "only_active": {"type": "boolean", "description": "Hide paused/ended campaigns. Default true."},
                "limit": {"type": "integer", "description": "Max rows to return. Default 20."},
            },
        },
    },
    list_campaigns_impl,
)


# ----------------------------------------------------------------------
# Tool: scan_campaigns
# ----------------------------------------------------------------------
def scan_campaigns_impl() -> str:
    """Run the Whop scanner to refresh the campaign list."""
    r = subprocess.run(
        [PYTHON, "-m", "scanner", "--debug"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=600,
    )
    tail = "\n".join((r.stdout or "").splitlines()[-15:])
    return f"<b>Scan finished (exit {r.returncode}).</b>\n<pre>{_esc(tail)}</pre>"


_register(
    {
        "name": "scan_campaigns",
        "description": "Re-scan Whop's Clip Farm for fresh campaigns. Takes about a minute. "
                       "Use when user asks 'scan for new campaigns', 'refresh', 'check for new'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    scan_campaigns_impl,
)


# ----------------------------------------------------------------------
# Tool: extract_brief
# ----------------------------------------------------------------------
def extract_brief_impl(campaign_id: int, force: bool = False) -> str:
    args = [PYTHON, "scripts/auto_extract_briefs.py", "--id", str(campaign_id)]
    if force:
        args.append("--force")
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=600)
    tail = "\n".join((r.stdout or "").splitlines()[-10:])
    return f"<b>extract_brief on #{campaign_id} (exit {r.returncode}):</b>\n<pre>{_esc(tail)}</pre>"


_register(
    {
        "name": "extract_brief",
        "description": "Auto-pull a campaign's brief from Whop + any linked Google Docs, then "
                       "extract structured rules via Claude. Use when the user wants to 'get the rules' "
                       "for a campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "force": {"type": "boolean", "description": "Re-fetch even if brief already exists."},
            },
            "required": ["campaign_id"],
        },
    },
    extract_brief_impl,
)


# ----------------------------------------------------------------------
# Tool: find_source
# ----------------------------------------------------------------------
def find_source_impl(campaign_id: int, download: bool = False) -> str:
    args = [PYTHON, "scripts/find_source.py", str(campaign_id)]
    if download:
        args.append("--download")
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=900)
    tail = "\n".join((r.stdout or "").splitlines()[-12:])
    return f"<b>find_source on #{campaign_id} (exit {r.returncode}):</b>\n<pre>{_esc(tail)}</pre>"


_register(
    {
        "name": "find_source",
        "description": "Find a YouTube source video matching the campaign brief. With download=true, "
                       "also fetches it and registers it as the campaign's current source. Only works for "
                       "campaigns without source_must_match restrictions (#42 Enhanced Games, #44 Jacks, #46 Anyma).",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "download": {"type": "boolean", "description": "Also download the picked source. Default false."},
            },
            "required": ["campaign_id"],
        },
    },
    find_source_impl,
)


# ----------------------------------------------------------------------
# Tool: run_pipeline
# ----------------------------------------------------------------------
def run_pipeline_impl(
    campaign_id: int,
    source_path: Optional[str] = None,
    format_mode: Optional[str] = None,
    auto_submit: bool = True,
    n_clips: Optional[int] = 1,
) -> str:
    global _PIPELINE_THREAD
    # Concurrency guard: only one clip pipeline at a time.
    if _is_pipeline_running():
        return (
            "⏳ A clip pipeline is already running. Wait for it to finish "
            "(you'll get the Telegram approval prompt + final summary), or "
            "kill it manually if it's stuck."
        )

    # Resolve format_mode from the campaign's stored default if the caller
    # didn't specify one. Podcasts default to blur_pad (wide-angle, all
    # speakers visible); food / demo / single-presenter default to crop.
    if not format_mode:
        rows = _q("SELECT format_mode_default FROM campaigns WHERE id = ?", (campaign_id,))
        if rows and rows[0].get("format_mode_default"):
            format_mode = rows[0]["format_mode_default"]
        else:
            format_mode = "crop"
    args = [PYTHON, "-m", "orchestrator", "--campaign", str(campaign_id), "--format-mode", format_mode]
    if auto_submit:
        args.append("--auto-submit")
    if n_clips:
        args += ["--n-clips", str(n_clips)]
    if source_path:
        args += ["--source", source_path]
    else:
        # Use the campaign's registered source
        rows = _q("SELECT current_source_path FROM campaigns WHERE id = ?", (campaign_id,))
        if not rows or not (rows[0].get("current_source_path") or "").strip():
            return (
                f"⚠️ Campaign #{campaign_id} has no registered source. "
                f"Use find_source first (with download=true) or pass a source_path."
            )
        args += ["--source", rows[0]["current_source_path"]]

    # Background runner — bot's main loop stays responsive while clip work
    # happens. We send the final summary to Telegram when the subprocess
    # finishes; in the meantime Chris can chat normally with the bot.
    def _worker():
        global _PIPELINE_THREAD
        try:
            logger.info(f"[bot] running pipeline (bg): {' '.join(args)}")
            r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60 * 60)
            tail = "\n".join((r.stdout or "").splitlines()[-15:])
            _bg_notify_telegram(
                f"<b>Pipeline on #{campaign_id} finished (exit {r.returncode}).</b>\n<pre>{_esc(tail)}</pre>"
            )
        except subprocess.TimeoutExpired:
            _bg_notify_telegram(
                f"⚠️ Pipeline on #{campaign_id} timed out after 60 min. May be stuck — check the bot log."
            )
        except Exception as e:
            logger.exception(f"[bot] pipeline worker crashed: {e}")
            _bg_notify_telegram(f"⚠️ Pipeline on #{campaign_id} crashed: <code>{_esc(str(e))}</code>")

    _PIPELINE_THREAD = threading.Thread(target=_worker, daemon=True, name=f"pipeline-{campaign_id}")
    _PIPELINE_THREAD.start()
    return (
        f"🎬 <b>Pipeline started for #{campaign_id}</b> in <code>{format_mode}</code> mode. "
        "You'll get Telegram approval prompts as clips are ready. "
        "Meanwhile I'm still listening — message me anytime."
    )


def _bg_notify_telegram(text: str) -> None:
    """Send a status update from the background pipeline worker."""
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not (bot_token and chat_id):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
    except Exception as e:
        logger.warning(f"[bot] bg notify failed: {e}")


_register(
    {
        "name": "run_pipeline",
        "description": "Produce + post + (optionally) auto-submit ONE clip for a campaign. Default is "
                       "1 clip per call (so you can rotate across multiple campaigns/day rather than dumping "
                       "3 clips from the same channel). The Telegram approval gate fires for that clip. "
                       "Default format_mode is 'crop' for food/demo content; use 'blur_pad' for podcasts; "
                       "'smart' for vision-guided crop on single-speaker talking-head.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "source_path": {"type": "string", "description": "Override the registered source. Optional."},
                "format_mode": {"type": "string", "enum": ["smart", "crop", "blur_pad", "letterbox"]},
                "auto_submit": {"type": "boolean", "description": "Default true."},
                "n_clips": {"type": "integer", "description": "Number of clips to produce. Default 1."},
            },
            "required": ["campaign_id"],
        },
    },
    run_pipeline_impl,
)


# ----------------------------------------------------------------------
# Tool: run_next_clip — rotate across eligible campaigns
# ----------------------------------------------------------------------
def run_next_clip_impl(
    format_mode: Optional[str] = None,
    auto_submit: bool = True,
) -> str:
    """Pick the next eligible campaign (under daily quota, has source) and run one clip.

    `format_mode` defaults to whatever the picked campaign's stored
    `format_mode_default` is (blur_pad for podcasts, crop for food/demos).
    Pass an explicit value only to override that default.
    """
    from scheduler.profit_ranker import pick_next_campaign_by_ev
    repo = _repo()
    campaign = pick_next_campaign_by_ev(repo)
    if not campaign:
        return (
            "No eligible campaign right now. Either all are at today's quota (2/day), "
            "or none of the under-quota campaigns have a registered source. "
            "Use <code>find_source</code> + <code>--download</code> to register one, "
            "or message me <i>'show campaigns'</i>."
        )
    pick_reason = campaign.pop("_pick_reason", None)
    result = run_pipeline_impl(
        campaign_id=campaign["id"],
        format_mode=format_mode,   # None → run_pipeline_impl reads campaign default
        auto_submit=auto_submit,
        n_clips=1,
    )
    if pick_reason:
        return f"<b>📊 Picked by EV:</b> {pick_reason}\n\n{result}"
    return result


_register(
    {
        "name": "run_next_clip",
        "description": "Pick the next eligible campaign automatically (highest viability under daily 2-clip quota, "
                       "with a registered source) and produce ONE clip. Use when the user says "
                       "'run next clip', 'give me the next one', 'do today's slot', etc. Rotates across "
                       "campaigns so you don't post 3 in a row from the same channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "format_mode": {"type": "string", "enum": ["smart", "crop", "blur_pad", "letterbox"]},
                "auto_submit": {"type": "boolean"},
            },
        },
    },
    run_next_clip_impl,
)


# ----------------------------------------------------------------------
# Tool: show_recent_posts
# ----------------------------------------------------------------------
def show_recent_posts_impl(limit: int = 10) -> str:
    rows = _q(
        "SELECT p.id, p.platform, p.post_url, p.posted_at, cl.caption_text, c.title AS campaign_title "
        "FROM posts p "
        "LEFT JOIN clips cl ON cl.id = p.clip_id "
        "LEFT JOIN campaigns c ON c.id = cl.campaign_id "
        "WHERE p.status='posted' AND p.post_url IS NOT NULL "
        "ORDER BY p.posted_at DESC LIMIT ?",
        (limit,),
    )
    if not rows:
        return "No posts yet."
    lines = [f"<b>Last {len(rows)} post(s):</b>"]
    for r in rows:
        title = (r.get("caption_text") or "").split("\n", 1)[0][:60]
        when = (r.get("posted_at") or "")[:16].replace("T", " ")
        lines.append(f"  • {r['platform']:9s} {when}  {title}\n     {r['post_url']}")
    return "\n".join(lines)


_register(
    {
        "name": "show_recent_posts",
        "description": "List the most recent platform posts (YouTube / Instagram / TikTok) with URLs.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Default 10."}},
        },
    },
    show_recent_posts_impl,
)


# ----------------------------------------------------------------------
# Tool: show_earnings_estimate
# ----------------------------------------------------------------------
def show_earnings_estimate_impl() -> str:
    """Best-effort earnings estimate: latest analytics row per post × CPM."""
    rows = _q(
        "SELECT p.id AS post_id, p.platform, p.post_url, c.payout_per_1k_views AS cpm, "
        "(SELECT MAX(views) FROM analytics WHERE post_id = p.id) AS views "
        "FROM posts p "
        "LEFT JOIN clips cl ON cl.id = p.clip_id "
        "LEFT JOIN campaigns c ON c.id = cl.campaign_id "
        "WHERE p.status='posted' AND p.post_url IS NOT NULL "
        "ORDER BY p.posted_at DESC"
    )
    if not rows:
        return "No posted clips to estimate yet."
    total = 0.0
    lines = ["<b>Earnings estimate (latest snapshot × CPM):</b>"]
    for r in rows:
        cpm = r.get("cpm") or 0.0
        views = r.get("views") or 0
        est = (views / 1000.0) * cpm if cpm else 0.0
        total += est
        lines.append(f"  {r['platform']:9s} #{r['post_id']}: {views:,} views × ${cpm}/1k = ${est:.2f}")
    lines.append(f"\n<b>Total estimate:</b> ${total:.2f}")
    lines.append("<i>(Real payouts come from Whop after review — this is a Floor estimate from cached analytics.)</i>")
    return "\n".join(lines)


_register(
    {
        "name": "show_earnings_estimate",
        "description": "Best-effort estimate of clip earnings based on cached view counts × CPM. "
                       "Use when user asks 'how much have I earned', 'earnings', 'revenue'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    show_earnings_estimate_impl,
)


# ----------------------------------------------------------------------
# Tool: track_analytics
# ----------------------------------------------------------------------
def track_analytics_impl(hours: Optional[int] = None) -> str:
    args = [PYTHON, "scripts/track_analytics.py"]
    if hours:
        args += ["--hours", str(hours)]
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300)
    tail = "\n".join((r.stdout or "").splitlines()[-15:])
    return f"<b>Analytics refresh (exit {r.returncode}):</b>\n<pre>{_esc(tail)}</pre>"


_register(
    {
        "name": "track_analytics",
        "description": "Refresh view/like/comment counts for all posted clips. Pulls from YT Data API "
                       "and IG Graph API.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Only refresh posts from last N hours."}},
        },
    },
    track_analytics_impl,
)


# ----------------------------------------------------------------------
# Tool: notify_48hr
# ----------------------------------------------------------------------
def notify_48hr_impl(dry_run: bool = False) -> str:
    args = [PYTHON, "scripts/notify_48hr_screenshots.py"]
    if dry_run:
        args.append("--dry-run")
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
    tail = "\n".join((r.stdout or "").splitlines()[-10:])
    return f"<b>48hr screenshot ping (exit {r.returncode}):</b>\n<pre>{_esc(tail)}</pre>"


_register(
    {
        "name": "notify_48hr",
        "description": "Fire the 48hr screenshot Telegram reminders for any posts that crossed the line.",
        "input_schema": {
            "type": "object",
            "properties": {"dry_run": {"type": "boolean", "description": "Print without sending. Default false."}},
        },
    },
    notify_48hr_impl,
)


# ----------------------------------------------------------------------
# Tool: system_status
# ----------------------------------------------------------------------
def system_status_impl() -> str:
    """Quick health snapshot — campaigns, recent posts, today's clip count."""
    campaigns = _q(
        "SELECT COUNT(*) AS n FROM campaigns WHERE status IS NULL OR status='active'"
    )[0]["n"]
    today_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    posts_today = _q(
        "SELECT COUNT(*) AS n FROM posts WHERE status='posted' AND posted_at >= ?",
        (today_cutoff,),
    )[0]["n"]
    pending_48 = _q(
        "SELECT COUNT(*) AS n FROM posts WHERE status='posted' AND posted_at <= ? "
        "AND (analytics_48hr_notified_at IS NULL OR analytics_48hr_notified_at = '')",
        ((datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds"),),
    )[0]["n"]
    return (
        f"<b>System status</b>\n"
        f"  Active campaigns: {campaigns}\n"
        f"  Posts in last 24h: {posts_today}\n"
        f"  Pending 48hr screenshot pings: {pending_48}\n"
    )


_register(
    {
        "name": "system_status",
        "description": "Quick health snapshot of the clipfarmer system. Use for 'how's it going', "
                       "'status', 'what's the state of things'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    system_status_impl,
)


# ----------------------------------------------------------------------
# Tool: get_email_code
# ----------------------------------------------------------------------
def get_email_code_impl(sender: str, max_age_minutes: int = 5) -> str:
    """Pull a verification code from the burner Gmail."""
    from engine.email_fetcher import get_latest_code
    code = get_latest_code(sender_contains=sender, max_age_seconds=max_age_minutes * 60)
    if not code:
        return (f"No fresh code from <code>{sender}</code> in the last {max_age_minutes} min. "
                f"Either no email arrived, or BURNER_EMAIL_USER / BURNER_EMAIL_PASSWORD "
                f"aren't set in <code>.env</code>.")
    return f"<b>Code from {sender}:</b> <code>{code}</code>"


_register(
    {
        "name": "get_email_code",
        "description": "Fetch the most recent verification code from the burner Gmail inbox. "
                       "Pass `sender` as a substring of the sender's email/name (e.g. 'tiktok', "
                       "'clipstake', 'discord'). Use for 'get my tiktok code', 'pull the clipstake code'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Substring of the sender's email/name"},
                "max_age_minutes": {"type": "integer", "description": "Max age in minutes (default 5)"},
            },
            "required": ["sender"],
        },
    },
    get_email_code_impl,
)


# ----------------------------------------------------------------------
# Tool: how_am_i_doing
# ----------------------------------------------------------------------
def how_am_i_doing_impl() -> str:
    """Single-message snapshot of system health + recent earnings."""
    from engine.health_check import run_all_checks
    from datetime import datetime, timedelta, timezone
    repo = _repo()
    cutoff_24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")

    with repo.conn() as c:
        # Posts in last 24h + 7d
        n24 = c.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE status='posted' AND posted_at >= ?",
            (cutoff_24,),
        ).fetchone()["n"]
        n7 = c.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE status='posted' AND posted_at >= ?",
            (cutoff_7d,),
        ).fetchone()["n"]
        # Total views from recent posts
        v24 = c.execute(
            "SELECT SUM(v.mx) AS s FROM ("
            "  SELECT p.id, MAX(a.views) AS mx FROM posts p "
            "  JOIN analytics a ON a.post_id = p.id "
            "  WHERE p.posted_at >= ? GROUP BY p.id"
            ") v", (cutoff_24,),
        ).fetchone()["s"] or 0
        v7 = c.execute(
            "SELECT SUM(v.mx) AS s FROM ("
            "  SELECT p.id, MAX(a.views) AS mx FROM posts p "
            "  JOIN analytics a ON a.post_id = p.id "
            "  WHERE p.posted_at >= ? GROUP BY p.id"
            ") v", (cutoff_7d,),
        ).fetchone()["s"] or 0
        # Active campaign count
        n_camps = c.execute(
            "SELECT COUNT(*) AS n FROM campaigns WHERE status='active' OR status IS NULL"
        ).fetchone()["n"]
        # Submissions
        n_subs = c.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE submitted_at >= ?",
            (cutoff_7d,),
        ).fetchone()["n"]

    # Implied earnings — sum views × CPM per campaign
    implied_24 = implied_7 = 0.0
    with repo.conn() as c:
        rows = c.execute(
            "SELECT cl.campaign_id, p.posted_at, MAX(a.views) AS v "
            "FROM posts p JOIN clips cl ON cl.id = p.clip_id "
            "JOIN analytics a ON a.post_id = p.id "
            "WHERE p.posted_at >= ? GROUP BY p.id",
            (cutoff_7d,),
        ).fetchall()
        # Cache CPMs per campaign
        cpms: dict[int, float] = {}
        for r in rows:
            cid = r["campaign_id"]
            if cid not in cpms:
                row = c.execute("SELECT payout_per_1k_views FROM campaigns WHERE id=?", (cid,)).fetchone()
                cpms[cid] = float(row["payout_per_1k_views"] or 0.50) if row else 0.50
            v = int(r["v"] or 0)
            earned = v * cpms[cid] / 1000.0
            implied_7 += earned
            if r["posted_at"] and r["posted_at"] >= cutoff_24:
                implied_24 += earned

    # Health
    results = run_all_checks()
    n_ok = sum(1 for r in results if r.ok)
    n_total = len(results)
    health_icon = "🟢" if n_ok == n_total else ("🟡" if n_ok >= n_total - 2 else "🔴")

    return (
        f"<b>📊 How you're doing</b>\n\n"
        f"<b>Last 24h:</b>  {n24} post(s) · {v24:,} views · ~${implied_24:.2f}\n"
        f"<b>Last 7d:</b>   {n7} post(s) · {v7:,} views · ~${implied_7:.2f}\n"
        f"<b>Submissions (7d):</b> {n_subs}\n"
        f"<b>Active campaigns:</b> {n_camps}\n"
        f"<b>Health:</b> {health_icon} {n_ok}/{n_total} integrations OK"
    )


_register(
    {
        "name": "how_am_i_doing",
        "description": "Quick snapshot — posts, views, implied earnings (24h + 7d), submissions, "
                       "and integration health. Use for 'how am I doing', 'status', 'progress'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    how_am_i_doing_impl,
)


# ----------------------------------------------------------------------
# Tool: show_experiments
# ----------------------------------------------------------------------
def show_experiments_impl(campaign_id: Optional[int] = None) -> str:
    """Show queued + recent Brain experiments for one campaign or all."""
    from engine.brain.experimenter import get_queued_experiment
    repo = _repo()
    if campaign_id is not None:
        ids = [campaign_id]
    else:
        with repo.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM campaigns WHERE status='active' OR status IS NULL"
            ).fetchall()]

    lines = ["<b>🧪 Brain experiments</b>"]
    any_queued = False
    for cid in ids:
        with repo.conn() as c:
            row = c.execute(
                "SELECT title, experiments FROM campaigns WHERE id=?", (cid,)
            ).fetchone()
        if not row:
            continue
        title = row["title"][:40]
        qexp = get_queued_experiment(repo, cid)
        try:
            exp_payload = json.loads(row["experiments"]) if row["experiments"] else {}
        except Exception:
            exp_payload = {}
        props = exp_payload.get("proposals") or []
        if not qexp and not props:
            continue
        any_queued = any_queued or bool(qexp)
        lines.append(f"\n<b>#{cid} {title}</b>")
        if qexp:
            lines.append(f"   🧪 <b>Queued:</b> {qexp.get('hypothesis', '')[:140]}")
            params = qexp.get("system_params") or {}
            if params:
                lines.append(f"   ⚙️ Params: <code>{json.dumps(params)[:140]}</code>")
            lines.append(f"   💡 Expected: {qexp.get('expected_outcome', '')[:140]}")
        elif props:
            lines.append(f"   (none queued; {len(props)} historical proposal{'s' if len(props) != 1 else ''})")
    if len(lines) == 1:
        return "No experiments queued or proposed yet. Run <code>propose_experiments</code> to generate some."
    if not any_queued:
        lines.append("\n<i>Nothing currently queued. Next nightly job re-proposes.</i>")
    return "\n".join(lines)


_register(
    {
        "name": "show_experiments",
        "description": "Show Brain experiments — what's queued for the next explore slot per campaign and "
                       "what proposals exist. Use for 'what's the brain testing', 'show experiments', "
                       "'what's queued for #43'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
            },
        },
    },
    show_experiments_impl,
)


# ----------------------------------------------------------------------
# Tool: cancel_experiment
# ----------------------------------------------------------------------
def cancel_experiment_impl(campaign_id: int) -> str:
    """Clear the queued experiment for a campaign so the next explore slot picks fresh."""
    from engine.brain.experimenter import get_queued_experiment, clear_queued_experiment
    repo = _repo()
    cur = get_queued_experiment(repo, campaign_id)
    if not cur:
        return f"No experiment queued for #{campaign_id}."
    clear_queued_experiment(repo, campaign_id)
    return f"✅ Cancelled queued experiment for #{campaign_id}: <i>{cur.get('hypothesis', '')[:140]}</i>"


_register(
    {
        "name": "cancel_experiment",
        "description": "Cancel the experiment queued for a campaign. Use when Chris says 'cancel the experiment for #43' "
                       "or 'don't test that on Boxabl'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
            },
            "required": ["campaign_id"],
        },
    },
    cancel_experiment_impl,
)


# ----------------------------------------------------------------------
# Tool: show_experiment_outcomes
# ----------------------------------------------------------------------
def show_experiment_outcomes_impl() -> str:
    """Run the experiment-outcomes attributor and return a summary."""
    from engine.brain.experiment_outcomes import refresh_outcomes
    repo = _repo()
    out = refresh_outcomes(repo)
    if not out:
        return "No experiments have outcomes yet. Either no posts under experiments, or analytics aren't in."
    lines = ["<b>📊 Brain experiment outcomes</b>"]
    for v in out:
        icon = {"hit": "🎯", "miss": "❌", "neutral": "➖"}.get(v["verdict"], "•")
        lines.append(
            f"\n{icon} <b>#{v['campaign_id']}</b> · {v['verdict'].upper()} · {v['lift']:.2f}× baseline "
            f"({v['views']:,} views)\n   <i>{v['hypothesis'][:140]}</i>"
        )
    return "\n".join(lines)


_register(
    {
        "name": "show_experiment_outcomes",
        "description": "Show whether the Brain's recent experiments hit, missed, or were neutral. Use for "
                       "'did the experiments work', 'show experiment results', 'how are the tests doing'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    show_experiment_outcomes_impl,
)


# ----------------------------------------------------------------------
# Tool: add_clipify_campaign
# ----------------------------------------------------------------------
def add_clipify_campaign_impl(
    server_name: str,
    campaign_brief: str,
    cpm_usd: Optional[float] = None,
    budget_total: Optional[float] = None,
    budget_remaining_pct: Optional[float] = None,
    format_mode_default: Optional[str] = None,
) -> str:
    """Add a Clipify per-streamer server as a campaign in the DB. Runs the
    pasted #campaign-details text through the same rules extractor we use
    for Whop campaigns so hashtags / mentions / forbidden phrases / platforms
    get captured automatically."""
    if not server_name or not campaign_brief or len(campaign_brief.strip()) < 50:
        return "Need both a server name AND the full #campaign-details text (paste it in full)."

    from engine.rules_extractor import extract_rules, RulesExtractionError
    from datetime import datetime, timezone

    title = server_name.strip()
    repo = _repo()
    # whop_campaign_id is the unique key — namespace by marketplace to avoid
    # collisions with existing Whop campaigns.
    whop_id = f"clipify::{title}"

    try:
        rules = extract_rules(campaign_brief, campaign_title=title)
    except RulesExtractionError as e:
        return f"⚠️ Couldn't extract rules from the brief: {e}"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with repo.conn() as c:
        existing = c.execute(
            "SELECT id FROM campaigns WHERE whop_campaign_id = ?", (whop_id,),
        ).fetchone()
        if existing:
            cid = existing["id"]
            c.execute(
                "UPDATE campaigns SET title=?, marketplace=?, marketplace_server=?, "
                "campaign_brief=?, structured_rules=?, payout_per_1k_views=?, "
                "budget_total=?, budget_remaining_pct=?, format_mode_default=?, "
                "last_seen_at=? WHERE id=?",
                (
                    title, "clipify", server_name.strip(),
                    campaign_brief, json.dumps(rules),
                    cpm_usd, budget_total, budget_remaining_pct,
                    format_mode_default, now, cid,
                ),
            )
            action = "updated"
        else:
            c.execute(
                "INSERT INTO campaigns ("
                "whop_campaign_id, community_id, community_name, title, "
                "marketplace, marketplace_server, "
                "payout_per_1k_views, budget_total, budget_remaining_pct, "
                "campaign_brief, structured_rules, format_mode_default, "
                "status, discovered_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    whop_id, "clipify", "Clipify", title,
                    "clipify", server_name.strip(),
                    cpm_usd, budget_total, budget_remaining_pct,
                    campaign_brief, json.dumps(rules), format_mode_default,
                    "active", now, now,
                ),
            )
            cid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            action = "added"

    summary = [
        f"<b>✅ Clipify campaign {action}: #{cid} {title}</b>",
    ]
    if rules.get("required_hashtags"):
        summary.append(f"• Required hashtags: {' '.join(rules['required_hashtags'])}")
    if rules.get("required_mentions"):
        summary.append(f"• Required mentions: {' '.join(rules['required_mentions'])}")
    if rules.get("platforms_required"):
        summary.append(f"• Platforms: {', '.join(rules['platforms_required'])}")
    if cpm_usd:
        summary.append(f"• CPM: ${cpm_usd}/1k")
    summary.append("")
    summary.append("Next: register a source video (find_source or local file) then run a clip.")
    return "\n".join(summary)


def show_learnings_impl(campaign_id: Optional[int] = None) -> str:
    """Show what the Brain has learned about a campaign — winning patterns,
    baseline median views, lift per feature. Without an id, lists all
    campaigns that have learnings populated."""
    from engine.brain import refresh_learnings
    from engine.brain.advisor import human_summary
    from engine.brain.learnings import get_learnings
    repo = _repo()
    if campaign_id is None:
        with repo.conn() as c:
            rows = c.execute(
                "SELECT id, title, learnings FROM campaigns "
                "WHERE learnings IS NOT NULL AND learnings != '' "
                "ORDER BY id"
            ).fetchall()
        if not rows:
            return ("No learnings persisted yet. Run "
                    "<code>python scripts/refresh_learnings.py</code> "
                    "after you have 3+ posted-and-tracked clips per campaign.")
        out = [f"<b>🧠 Brain — {len(rows)} campaign(s) with learnings</b>", ""]
        for r in rows:
            out.append(human_summary(repo, r["id"]))
            out.append("")
        return "\n".join(out)
    # Specific campaign — refresh first so the latest analytics get folded in.
    refresh_learnings(repo, campaign_id=campaign_id)
    return human_summary(repo, campaign_id)


_register(
    {
        "name": "show_learnings",
        "description": "Show what the Brain has learned from posted-clip outcomes. "
                       "Without a campaign_id, lists every campaign with learnings; "
                       "with one, refreshes the learnings for that campaign and shows "
                       "its winning patterns (hook style, duration, hashtag count, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
            },
        },
    },
    show_learnings_impl,
)


def scan_clipify_directory_impl(dry_run: bool = False) -> str:
    """Scan Sx Bot Clipify's #active-campaigns and per-streamer servers,
    auto-add new campaigns to the DB with marketplace='clipify'."""
    from config import settings
    if not (settings.discord_burner_email and settings.discord_burner_password):
        return ("Burner Discord creds missing — add <code>DISCORD_BURNER_EMAIL</code> "
                "and <code>DISCORD_BURNER_PASSWORD</code> to <code>.env</code> first.")
    from scanner.discord_session import DiscordSession
    from scanner.clipify_directory import ClipifyDirectoryScanner
    repo = _repo()
    with DiscordSession() as ds:
        with ClipifyDirectoryScanner(session=ds) as scanner:
            entries = scanner.scan_directory()
            if dry_run:
                lines = [f"<b>📋 Directory dry-run: {len(entries)} entries</b>", ""]
                for e in entries[:30]:
                    inv = e.server_invite or "(no invite link)"
                    lines.append(f"• {e.streamer_name} → {inv}")
                return "\n".join(lines)
            result = scanner.ingest_into_db(repo, entries)
    lines = [f"<b>🛰 Clipify directory scan</b>", ""]
    lines.append(f"• Added: {len(result['added'])}")
    for s in result["added"][:10]:
        lines.append(f"    + {s}")
    lines.append(f"• Updated: {len(result['updated'])}")
    lines.append(f"• Need-to-join: {len(result['not_joined'])}")
    for e in result["not_joined"][:10]:
        inv = e.server_invite or "(no link)"
        lines.append(f"    ? {e.streamer_name}: {inv}")
    return "\n".join(lines)


_register(
    {
        "name": "scan_clipify_directory",
        "description": "Scan Sx Bot Clipify's #active-campaigns channel for new streamer campaigns. "
                       "For each entry, if the burner has already joined that streamer's per-server "
                       "Clipify, scrape #campaign-details and add the campaign to the DB. Servers the "
                       "burner hasn't joined yet are reported back so Chris can join them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Just list entries; don't ingest."},
            },
        },
    },
    scan_clipify_directory_impl,
)


_register(
    {
        "name": "add_clipify_campaign",
        "description": "Add a Clipify per-streamer Discord campaign to the system. Chris pastes the "
                       "Discord server's #campaign-details text (rules, hashtags, payouts, etc.) and "
                       "the system extracts structured rules + stores it as a campaign with "
                       "marketplace='clipify'. Future clips for this campaign generate copy-paste "
                       "/clips add commands instead of trying to fill a web form.",
        "input_schema": {
            "type": "object",
            "properties": {
                "server_name": {"type": "string", "description": "Name of the Clipify Discord server, e.g. 'Viptoria X Clipify'"},
                "campaign_brief": {"type": "string", "description": "Full pasted text from the server's #campaign-details channel."},
                "cpm_usd": {"type": "number", "description": "Pay rate in USD per 1k views (e.g. 0.50 if pay is $25 per 50k views)."},
                "budget_total": {"type": "number", "description": "Total campaign budget in USD if known."},
                "budget_remaining_pct": {"type": "number", "description": "Remaining budget percent (0-100) if visible."},
                "format_mode_default": {"type": "string", "enum": ["smart", "crop", "blur_pad", "letterbox"], "description": "Default 9:16 layout for this campaign. Use 'blur_pad' for podcasts, 'crop' for food/demo/walkthrough."},
            },
            "required": ["server_name", "campaign_brief"],
        },
    },
    add_clipify_campaign_impl,
)


# ----------------------------------------------------------------------
# Tool: capture_yt_studio_analytics
# ----------------------------------------------------------------------
def capture_yt_studio_analytics_impl(
    post_id: Optional[int] = None,
    video_id: Optional[str] = None,
    all_tabs: bool = True,
) -> str:
    """Open YT Studio in the cached Chrome profile, screenshot Audience (or all tabs)."""
    args = [PYTHON, "scripts/capture_yt_studio.py"]
    if post_id is not None:
        args += ["--post", str(post_id)]
    elif video_id:
        args += ["--video", video_id]
    else:
        return "Need a post_id or video_id."
    if all_tabs:
        args.append("--all-tabs")
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=600)
    tail = "\n".join((r.stdout or "").splitlines()[-15:])
    if r.returncode == 0:
        # The screenshots also get telegram-sent so Chris can forward to Whop.
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        import re
        # Parse saved PNG paths from stdout.
        saved = re.findall(r"(?:Saved|^\s+)(?::\s*)?(C:\\\S+\.png)", r.stdout or "")
        if gate.enabled and saved:
            try:
                import requests
                for png_path in saved:
                    p = Path(png_path)
                    if not p.exists():
                        continue
                    with p.open("rb") as fh:
                        requests.post(
                            f"https://api.telegram.org/bot{gate.bot_token}/sendPhoto",
                            data={
                                "chat_id": gate.chat_id,
                                "caption": f"<b>YT Studio</b> · <code>{p.name}</code>",
                                "parse_mode": "HTML",
                            },
                            files={"photo": (p.name, fh, "image/png")},
                            timeout=120,
                        )
            except Exception as e:
                logger.warning(f"[bot] couldn't telegram-send YT screenshots: {e}")
        return f"<b>YT Studio capture complete.</b>\n<pre>{_esc(tail)}</pre>"
    return f"⚠️ Capture failed:\n<pre>{_esc(r.stderr[-1500:])}</pre>"


_register(
    {
        "name": "capture_yt_studio_analytics",
        "description": "Open YouTube Studio in the saved Chrome profile, navigate to a video's Analytics "
                       "tabs, and screenshot Audience (and optionally Overview/Reach/Engagement too). "
                       "Sends the PNGs to Telegram so Chris can forward them to Whop's support chat. "
                       "Use when the user asks for analytics screenshots, mentions a Whop rejection that "
                       "needs analytics follow-up, or says 'get screenshots for post N'. "
                       "FIRST RUN will pop a browser asking the user to log into YT Studio — make sure "
                       "they know they need to interact with their machine briefly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "integer", "description": "DB post id (preferred — looks up the YT video id)."},
                "video_id": {"type": "string", "description": "Raw YouTube video id if you have it directly."},
                "all_tabs": {"type": "boolean", "description": "Default true. Capture all four analytics tabs."},
            },
        },
    },
    capture_yt_studio_analytics_impl,
)


# ----------------------------------------------------------------------
# Tool: show_dashboard
# ----------------------------------------------------------------------
def show_dashboard_impl(open_browser: bool = True) -> str:
    args = [PYTHON, "scripts/dashboard.py"]
    if not open_browser:
        args.append("--no-open")
    r = subprocess.run(args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
    out_path = PROJECT_ROOT / "data" / "dashboard.html"
    if r.returncode == 0:
        return (
            f"<b>Dashboard refreshed.</b>\n"
            f"<code>{out_path}</code>\n"
            f"{'(opened in your browser)' if open_browser else '(open it manually to view)'}"
        )
    return f"⚠️ Dashboard render failed:\n<pre>{_esc(r.stderr[-1000:])}</pre>"


_register(
    {
        "name": "show_dashboard",
        "description": "Regenerate the HTML dashboard with latest stats and open it in the browser. "
                       "Use for 'show me the dashboard', 'refresh dashboard', 'how's revenue looking?' "
                       "(when a visual answer is better than text).",
        "input_schema": {
            "type": "object",
            "properties": {
                "open_browser": {"type": "boolean", "description": "Open in default browser. Default true."},
            },
        },
    },
    show_dashboard_impl,
)


# ----------------------------------------------------------------------
# Tool: show_schedule
# ----------------------------------------------------------------------
# The background scheduler is started in bot.__main__ and stored on the
# ChatAgent instance after creation. We look it up via a module-level
# reference set by bot.__main__.
SCHEDULER_REF: Any = None


def show_schedule_impl() -> str:
    s = SCHEDULER_REF
    if s is None:
        return "Background scheduler isn't running. Start the bot with <code>python -m bot</code>."
    jobs = s.get_jobs()
    if not jobs:
        return "Scheduler is up but has no jobs registered."
    lines = ["<b>Background jobs (next run in UTC):</b>"]
    for j in jobs:
        when = j.next_run_time.strftime("%Y-%m-%d %H:%M %Z") if j.next_run_time else "—"
        lines.append(f"  • <code>{j.id}</code> → {when}")
    lines.append("\n<i>Posting still requires your /approve — these are background-only jobs.</i>")
    return "\n".join(lines)


_register(
    {
        "name": "show_schedule",
        "description": "Show what jobs the background scheduler will run next (scan, brief extract, "
                       "tracking, 48hr pings). Use when user asks 'what's scheduled', 'what's next', "
                       "'when will the next scan happen'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    show_schedule_impl,
)


# ----------------------------------------------------------------------
def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
