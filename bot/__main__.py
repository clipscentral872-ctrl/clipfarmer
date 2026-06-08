"""Entry point for the natural-language Telegram chat agent.

Usage:
    python -m bot
"""

from __future__ import annotations

import sys

from loguru import logger

from config import settings
from .chat_agent import ChatAgent
from .schedule import start_background_scheduler


def main() -> int:
    logger.add(settings.logs_dir / "bot.log", rotation="20 MB", retention=10)
    agent = ChatAgent()
    # Background jobs run alongside the chat loop: scan, briefs, tracking,
    # 48hr screenshot pings. Posting is NOT scheduled here — Chris still
    # triggers clip runs explicitly via chat.
    sched = start_background_scheduler(notify=agent._send)
    try:
        agent.run_forever()
    finally:
        sched.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
