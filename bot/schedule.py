"""Background scheduler that runs inside the bot process.

The bot's main loop polls Telegram and answers messages. This scheduler
runs cron-style jobs in a background thread so the bot daemon also
handles all the periodic chores: refresh campaigns, auto-extract briefs,
refresh analytics, fire 48hr screenshot pings.

Jobs are functions (no subprocess) so they share the bot process's
state without spawning second Telegram pollers — no race condition.

Results are surfaced to Chris via the bot's existing notify channel
(direct Telegram message), so he sees what the daemon did overnight.

NOT scheduled here: posting slots / the orchestrator. Posting still
requires Chris's `/approve` for every clip — when he says "run a clip"
via chat, the bot's run_pipeline tool subprocesses the orchestrator
synchronously (so the orchestrator's own Telegram polling and the bot's
loop don't compete).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from config import settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
TG_BASE = "https://api.telegram.org"


# ----------------------------------------------------------------------
def start_background_scheduler(notify: Optional[Callable[[str], None]] = None) -> BackgroundScheduler:
    """Create + start the in-process scheduler. Returns the scheduler handle so
    callers can introspect / shut down on exit.

    `notify(text)` is called with a Telegram-formatted summary after each
    job that has user-relevant output (e.g. 48hr ping fired). If omitted
    we send to Telegram directly.
    """
    if notify is None:
        notify = _tg_send

    s = BackgroundScheduler(timezone="UTC")

    s.add_job(
        lambda: _job_track_analytics(notify),
        IntervalTrigger(hours=4),
        id="track_analytics",
        replace_existing=True,
    )
    s.add_job(
        lambda: _job_notify_48hr(notify),
        IntervalTrigger(hours=1),
        id="notify_48hr",
        replace_existing=True,
    )
    s.add_job(
        lambda: _job_scan(notify),
        CronTrigger(hour=2, minute=0),
        id="scan_campaigns",
        replace_existing=True,
    )
    s.add_job(
        lambda: _job_extract_briefs(notify),
        CronTrigger(hour=2, minute=15),
        id="extract_briefs",
        replace_existing=True,
    )

    s.start()
    logger.info("[bot/sched] background scheduler started")
    for job in s.get_jobs():
        logger.info(f"[bot/sched]   {job.id} → next run: {job.next_run_time}")
    return s


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------
def _job_track_analytics(notify: Callable[[str], None]) -> None:
    """Quietly refresh view counts. Only notify if there's something new."""
    try:
        from db.repository import Repository
        from engine.analytics_tracker import AnalyticsTracker
        repo = Repository()
        tracker = AnalyticsTracker()
        with repo.conn() as c:
            rows = c.execute(
                "SELECT * FROM posts WHERE status='posted' AND post_url IS NOT NULL"
            ).fetchall()
        ok = 0
        for r in rows:
            post = dict(r)
            snap = tracker.fetch_for_post(post)
            if snap is None:
                continue
            repo.record_analytics(post["id"], {
                "views": snap.views, "likes": snap.likes,
                "comments": snap.comments, "shares": snap.shares,
                "saves": snap.saves, "watch_time_sec": snap.watch_time_sec,
                "raw": snap.raw,
            })
            ok += 1
        logger.info(f"[bot/sched] track_analytics: refreshed {ok}/{len(rows)} posts")
    except Exception as e:
        logger.exception(f"[bot/sched] track_analytics crashed: {e}")


def _job_notify_48hr(notify: Callable[[str], None]) -> None:
    """Fire campaign-aware analytics screenshots. Uses the existing script
    which already respects per-campaign rules and renders YT PNGs."""
    try:
        r = subprocess.run(
            [PYTHON, "scripts/notify_48hr_screenshots.py"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=600,
        )
        out = (r.stdout or "").strip()
        if "Sent 0" not in out and out:
            # something actually went out
            logger.info(f"[bot/sched] notify_48hr: {out.splitlines()[-1] if out else ''}")
    except Exception as e:
        logger.exception(f"[bot/sched] notify_48hr crashed: {e}")


def _job_scan(notify: Callable[[str], None]) -> None:
    """Overnight refresh of campaign list."""
    try:
        r = subprocess.run(
            [PYTHON, "-m", "scanner", "--debug"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=1200,
        )
        if r.returncode == 0:
            notify("🌙 <b>Overnight scan complete.</b> Fresh campaign list in the DB.")
        else:
            notify(f"⚠️ <b>Overnight scan failed</b> (exit {r.returncode}). Check logs.")
    except Exception as e:
        logger.exception(f"[bot/sched] scan crashed: {e}")


def _job_extract_briefs(notify: Callable[[str], None]) -> None:
    """Auto-pull any new briefs after the overnight scan."""
    try:
        r = subprocess.run(
            [PYTHON, "scripts/auto_extract_briefs.py"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=1200,
        )
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        if "Done: 0 ok" in tail:
            return  # nothing new — stay quiet
        notify(f"🌙 <b>Auto-extracted briefs:</b>\n<pre>{_esc(tail)}</pre>")
    except Exception as e:
        logger.exception(f"[bot/sched] extract_briefs crashed: {e}")


# ----------------------------------------------------------------------
def _tg_send(text: str) -> None:
    """Telegram sendMessage fallback when no callback is passed in."""
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not (bot_token and chat_id):
        return
    try:
        requests.post(
            f"{TG_BASE}/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
    except Exception as e:
        logger.warning(f"[bot/sched] notify send failed: {e}")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
