"""24/7 orchestrator for clipfarmer.

Wires every module into APScheduler so the system runs hands-free:

    scan ─→ download ─→ transcribe ─→ score ─→ cut/caption/format
                                                       │
                                                       ▼
                                                publish (3 platforms)
                                                       │
                                                       ▼
                                        submit to Whop ─→ 48hr screenshot
                                                       │
                                                       ▼
                                                tracker ─→ learner
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from config import settings
from db import Repository
from db.migrations import init_db


def job_scan_whop(repo: Repository) -> None:
    logger.info("[scan] start")
    raise NotImplementedError


def job_process_pending_videos(repo: Repository) -> None:
    """download → transcribe → score → cut → caption → format."""
    logger.info("[engine] start")
    raise NotImplementedError


def job_publish_ready_clips(repo: Repository) -> None:
    """For each clip with status='ready', schedule a post on each platform."""
    logger.info("[publisher] start")
    raise NotImplementedError


def job_post_due(repo: Repository) -> None:
    """Find scheduled posts past their scheduled_for and upload them."""
    logger.info("[publisher.due] start")
    raise NotImplementedError


def job_submit_posted_clips(repo: Repository) -> None:
    """For each post that doesn't yet have a Whop submission, submit it."""
    logger.info("[submitter] start")
    raise NotImplementedError


def job_send_analytics_screenshots(repo: Repository) -> None:
    """For submissions older than N hours without a screenshot, send one."""
    logger.info("[submitter.analytics] start")
    raise NotImplementedError


def job_track_engagement(repo: Repository) -> None:
    logger.info("[tracker] start")
    raise NotImplementedError


def job_learn(repo: Repository) -> None:
    logger.info("[brain] start")
    raise NotImplementedError


def build_scheduler(repo: Repository) -> BlockingScheduler:
    sched = BlockingScheduler(timezone="UTC")

    sched.add_job(
        job_scan_whop, "interval",
        minutes=settings.scan_interval_minutes, args=[repo], id="scan_whop",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    sched.add_job(
        job_process_pending_videos, "interval",
        minutes=10, args=[repo], id="engine",
    )
    sched.add_job(
        job_publish_ready_clips, "interval",
        minutes=settings.post_interval_minutes, args=[repo], id="publisher",
    )
    sched.add_job(
        job_post_due, "interval",
        minutes=5, args=[repo], id="publisher_due",
    )
    sched.add_job(
        job_submit_posted_clips, "interval",
        minutes=15, args=[repo], id="submitter",
    )
    sched.add_job(
        job_send_analytics_screenshots, "interval",
        minutes=30, args=[repo], id="submitter_analytics",
    )
    sched.add_job(
        job_track_engagement, "interval",
        hours=2, args=[repo], id="tracker",
    )
    sched.add_job(
        job_learn, "interval",
        hours=12, args=[repo], id="brain",
    )

    return sched


def main() -> None:
    logger.add(settings.logs_dir / "clipfarmer.log", rotation="20 MB", retention=10)
    init_db()
    repo = Repository()
    sched = build_scheduler(repo)
    logger.info("clipfarmer scheduler starting")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("clipfarmer scheduler stopping")


if __name__ == "__main__":
    main()
