"""Quiet-hours gate for Telegram pings.

Chris's preference (2026-06-08): only deliver Telegram messages inside
15:00-21:30 SAST (his afternoon/evening). Anything generated outside that
window gets queued to disk and flushed when the window next opens.

Usage:
    from publisher.quiet_hours import is_in_active_window, enqueue, drain_queue

    if not is_in_active_window():
        enqueue({"kind": "notify", "text": message})
        return
    ...

Bypass the gate for genuinely urgent items with `urgent=True` on TelegramGate
calls — but default behaviour is to respect the window.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from config import settings


QUEUE_FILE = settings.project_root / ".auth" / "telegram_queue.jsonl"


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_in_active_window(now: Optional[datetime] = None) -> bool:
    """True if the current local time (in `quiet_hours_tz`) is inside the
    active window. False outside. Always True if quiet hours are disabled."""
    if not getattr(settings, "quiet_hours_enabled", False):
        return True
    try:
        tz = ZoneInfo(settings.quiet_hours_tz)
    except Exception:
        logger.warning(f"[quiet] unknown tz {settings.quiet_hours_tz!r}; treating as active")
        return True
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)
    start = _parse_hhmm(settings.quiet_hours_window_start)
    end = _parse_hhmm(settings.quiet_hours_window_end)
    t = now.time()
    if start <= end:
        return start <= t <= end
    # Wraps midnight (e.g. 22:00-06:00)
    return t >= start or t <= end


def enqueue(item: dict) -> None:
    """Append a queued telegram payload to the queue file. The payload is a
    dict with at minimum a 'kind' field (notify | photo | video)."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    item = dict(item)
    item.setdefault("queued_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with QUEUE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def queue_size() -> int:
    if not QUEUE_FILE.exists():
        return 0
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def drain_queue() -> list[dict]:
    """Return all queued items and atomically clear the file. The caller is
    responsible for actually delivering them — drain_queue itself does no IO
    beyond reading and deleting the file."""
    if not QUEUE_FILE.exists():
        return []
    items: list[dict] = []
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception as e:
                    logger.warning(f"[quiet] dropping unparseable queued line: {e}")
        QUEUE_FILE.unlink()
    except Exception as e:
        logger.warning(f"[quiet] drain_queue read failed: {e}")
    return items
