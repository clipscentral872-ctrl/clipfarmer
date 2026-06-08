"""Long-running scheduler.

Fires the right job at the right time so the daily 6-clips-across-3-campaigns
target runs without babysitting:

  scan          — every 6 h     (refresh campaign list, viability scores)
  brief_pull    — every 12 h    (auto_extract_briefs for new/missing campaigns)
  post slots    — 09/11/13/15/17/19 local  (one clip each, picker chooses campaign)
  track         — every 4 h     (refresh analytics for posted clips)
  pings_48hr    — every 1 h     (Telegram screenshot reminders)

Start it with:
    python -m scheduler

Stop with Ctrl-C. State (auth cookies, OAuth tokens, db) lives in this
project directory — no external infra required.
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import settings
from db.repository import Repository
from .quota import SLOT_TIMES_LOCAL
from .runner import run_one_slot


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run_script(args: list[str]) -> None:
    """Run a child script and stream its output into our logger.

    We use subprocess (not direct imports) for the scan / brief / track / 48hr
    jobs because they own their own logging configuration and we want them
    isolated from the scheduler process's memory.
    """
    cmd = [PYTHON, *args]
    logger.info(f"[sched] $ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60 * 30)
        if r.stdout:
            for line in r.stdout.splitlines():
                logger.info(f"[sched]   {line}")
        if r.stderr:
            for line in r.stderr.splitlines():
                logger.warning(f"[sched]   {line}")
        if r.returncode != 0:
            logger.warning(f"[sched] {args[0]} exited {r.returncode}")
    except Exception as e:
        logger.exception(f"[sched] job {args} crashed: {e}")


def job_scan() -> None:
    _run_script(["-m", "scanner", "--debug"])


def job_briefs() -> None:
    _run_script(["scripts/auto_extract_briefs.py"])


def job_post_slot() -> None:
    logger.info("[sched] posting slot fired")
    posted = run_one_slot(repo=Repository(), auto_submit=True)
    logger.info(f"[sched] slot result: {'posted' if posted else 'no-op'}")


def job_track() -> None:
    _run_script(["scripts/track_analytics.py"])


def job_48hr() -> None:
    _run_script(["scripts/notify_48hr_screenshots.py"])


def job_refresh_top_performers() -> None:
    """Daily refresh so the EV ranker sees current competitor view counts."""
    _run_script(["scripts/refresh_top_performers.py"])


def job_scan_clipify_directory() -> None:
    """Daily scan of Sx Bot Clipify's #active-campaigns to auto-ingest new streamer campaigns."""
    _run_script(["scripts/scan_clipify_directory.py"])


def job_refresh_learnings() -> None:
    """Nightly: aggregate posted-clip outcomes into per-campaign learnings."""
    _run_script(["scripts/refresh_learnings.py"])


def job_refresh_proposals() -> None:
    """Nightly: cross-campaign performance + promote/keep/demote/pause proposals."""
    _run_script(["scripts/refresh_proposals.py"])


def job_learn_competitors() -> None:
    """Metadata-only competitor analysis (fast)."""
    _run_script(["scripts/learn_competitors.py"])


def job_learn_competitors_deep() -> None:
    """Heavy: download + transcribe top performers; extract openers + pacing."""
    _run_script(["scripts/learn_competitors_deep.py"])


def job_propose_experiments() -> None:
    """Inventive experiment proposals + Telegram notify."""
    _run_script(["scripts/propose_experiments.py"])


def job_experiment_outcomes() -> None:
    """Attribute outcomes back to specific Brain experiments."""
    _run_script(["scripts/refresh_experiment_outcomes.py"])


def job_refresh_ig_token() -> None:
    """Daily IG token refresh — keeps the long-lived Page token alive."""
    _run_script(["scripts/refresh_ig_token.py"])


def job_daily_briefing() -> None:
    """Morning Telegram briefing: yesterday's earnings + today's plan + new opps."""
    _run_script(["scripts/send_daily_briefing.py"])


def job_scan_opportunities() -> None:
    """Hourly Vyro/ClipStake/ClipAffiliates scan; Telegram-alert on new high-EV campaigns."""
    _run_script(["scripts/scan_for_opportunities.py"])


def job_refresh_social_competitors() -> None:
    """Refresh competitor top_performers via YT/TT/IG real-app search."""
    _run_script(["scripts/refresh_social_competitors.py"])


def job_scrape_vyro_leaderboards() -> None:
    """Scrape Vyro campaign leaderboards for real competitor data."""
    _run_script(["scripts/scrape_vyro_leaderboards.py"])


def job_refresh_briefs() -> None:
    """Director: generate / refresh creative briefs per campaign."""
    _run_script(["scripts/refresh_briefs.py"])


def job_brain_reflection() -> None:
    """Weekly self-critique: Director EV calibration + AI score correlation."""
    _run_script(["scripts/brain_reflection.py"])


def job_cross_pattern() -> None:
    """Cross-campaign pattern transfer — feeds Director's prompts globally."""
    _run_script(["scripts/refresh_cross_patterns.py"])


def job_rejection_learning() -> None:
    """Extract patterns from rejected clips so the system avoids them."""
    _run_script(["scripts/learn_rejections.py"])


def job_learn_competitors() -> None:
    """Nightly: reverse-engineer top performers on each campaign panel."""
    _run_script(["scripts/learn_competitors.py"])


def job_flush_telegram_queue() -> None:
    """At the start of the active window, deliver every Telegram message
    queued during quiet hours."""
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if not gate.enabled:
            return
        n = gate.flush_queue()
        if n:
            logger.info(f"[sched] flushed {n} queued telegram message(s)")
    except Exception as e:
        logger.warning(f"[sched] telegram flush failed: {e}")


def main() -> int:
    logger.add(settings.logs_dir / "scheduler.log", rotation="20 MB", retention=10)
    logger.info("[sched] starting clipfarmer scheduler")

    # Target US viewer prime time — slots are written in US Eastern.
    # The scheduler observes DST automatically when timezone is named.
    s = BlockingScheduler(timezone="America/New_York")

    # Slots — interpret HH:MM as local time using APScheduler's default tz.
    for hhmm in SLOT_TIMES_LOCAL:
        hh, mm = hhmm.split(":")
        s.add_job(job_post_slot, CronTrigger(hour=int(hh), minute=int(mm)), id=f"post_{hhmm}", replace_existing=True)
        logger.info(f"[sched] registered post slot at {hhmm}")

    s.add_job(job_scan, CronTrigger(hour="*/6"), id="scan", replace_existing=True)
    s.add_job(job_briefs, CronTrigger(hour="2,14"), id="briefs", replace_existing=True)
    s.add_job(job_track, CronTrigger(hour="*/4"), id="track", replace_existing=True)
    s.add_job(job_48hr, CronTrigger(minute=15), id="48hr", replace_existing=True)
    # Refresh top-performer scrapes daily at 03:00 local so the EV ranker
    # has fresh competitor view counts before the 09:00 first posting slot.
    s.add_job(job_refresh_top_performers, CronTrigger(hour=3, minute=0),
              id="refresh_top", replace_existing=True)
    # Daily Clipify directory scan at 03:30 local so any new streamer
    # campaigns are in the DB before the 09:00 first posting slot.
    s.add_job(job_scan_clipify_directory, CronTrigger(hour=3, minute=30),
              id="scan_clipify_dir", replace_existing=True)
    # Brain refresh runs after analytics tracking (which itself runs every
    # 4 hours), nightly at 02:30 — gives us the latest outcomes before
    # the next morning's slots.
    s.add_job(job_refresh_learnings, CronTrigger(hour=2, minute=30),
              id="refresh_learnings", replace_existing=True)
    # Cross-campaign proposals after learnings (depends on per-campaign learnings).
    s.add_job(job_refresh_proposals, CronTrigger(hour=2, minute=45),
              id="refresh_proposals", replace_existing=True)
    # Competitor pipeline runs in sequence after top-performer scrape (03:00):
    s.add_job(job_learn_competitors, CronTrigger(hour=3, minute=20),
              id="learn_competitors_meta", replace_existing=True)
    s.add_job(job_learn_competitors_deep, CronTrigger(hour=4, minute=0),
              id="learn_competitors_deep", replace_existing=True)
    s.add_job(job_propose_experiments, CronTrigger(hour=5, minute=0),
              id="propose_experiments", replace_existing=True)
    # Attribute outcomes BEFORE proposing fresh experiments so the next
    # cycle benefits from yesterday's verdicts.
    s.add_job(job_experiment_outcomes, CronTrigger(hour=4, minute=45),
              id="experiment_outcomes", replace_existing=True)
    # IG token refresh: runs daily; the script no-ops unless the token is
    # within 7 days of expiry, so it's cheap to run every night.
    s.add_job(job_refresh_ig_token, CronTrigger(hour=4, minute=30),
              id="refresh_ig_token", replace_existing=True)
    # Morning briefing — 08:00 ET = ~15:00 SAST (Chris's afternoon)
    s.add_job(job_daily_briefing, CronTrigger(hour=8, minute=0),
              id="daily_briefing", replace_existing=True)
    # Hourly marketplace opportunity scan, with screenshot attached to alerts
    s.add_job(job_scan_opportunities, CronTrigger(minute=15),
              id="scan_opportunities", replace_existing=True)
    # Social-search competitor refresh — 06:00 ET daily. Hits real YT/TT/IG
    # app interfaces for current trending clips per active campaign topic.
    s.add_job(job_refresh_social_competitors, CronTrigger(hour=6, minute=0),
              id="refresh_social_competitors", replace_existing=True)
    # Vyro leaderboard scrape — runs every 6h (Vyro updates frequently)
    s.add_job(job_scrape_vyro_leaderboards, CronTrigger(hour="*/6", minute=20),
              id="scrape_vyro_lb", replace_existing=True)
    # Director: re-brief campaigns after competitor + learnings refresh, so
    # the brief reflects the freshest signal possible.
    # Brain meta-learning sequence — before Director briefs so they consume the latest:
    s.add_job(job_rejection_learning, CronTrigger(hour=5, minute=5),
              id="rejection_learning", replace_existing=True)
    s.add_job(job_cross_pattern, CronTrigger(hour=5, minute=15),
              id="cross_pattern", replace_existing=True)
    s.add_job(job_brain_reflection, CronTrigger(day_of_week="sun", hour=5, minute=25),
              id="brain_reflection", replace_existing=True)
    s.add_job(job_refresh_briefs, CronTrigger(hour=5, minute=30),
              id="refresh_briefs", replace_existing=True)
    # Competitor learning runs AFTER top-performer scraping (03:00),
    # to give the metadata fetch the latest panel URLs to chew on.
    s.add_job(job_learn_competitors, CronTrigger(hour=3, minute=15),
              id="learn_competitors", replace_existing=True)

    # Telegram quiet-hours flush — fires at the top of the active window
    # (15:00 SAST per config) and delivers everything queued overnight.
    try:
        from zoneinfo import ZoneInfo
        qtz = ZoneInfo(settings.quiet_hours_tz)
        sh, sm = settings.quiet_hours_window_start.split(":")
        s.add_job(
            job_flush_telegram_queue,
            CronTrigger(hour=int(sh), minute=int(sm), timezone=qtz),
            id="telegram_flush", replace_existing=True,
        )
        logger.info(f"[sched] queued telegram flush at {settings.quiet_hours_window_start} {settings.quiet_hours_tz}")
    except Exception as _e:
        logger.warning(f"[sched] couldn't register telegram flush: {_e}")

    # Pre-flight: announce any broken integration via Telegram on startup
    # so 24/7 mode never silently runs with a missing dependency.
    try:
        from engine.health_check import notify_if_failures
        notify_if_failures()
    except Exception as _e:
        logger.warning(f"[sched] health check failed (continuing): {_e}")
    # And run it daily at 07:30 ET so 24/7 stays healthy.
    s.add_job(lambda: _run_script(["scripts/health_check.py"]),
              CronTrigger(hour=7, minute=30),
              id="health_check", replace_existing=True)

    logger.info("[sched] running. Ctrl-C to stop.")
    try:
        s.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[sched] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
