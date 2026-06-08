"""Send the daily morning briefing to Telegram. Schedule for 08:00 ET."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.daily_briefing import send, build_briefing


def main() -> int:
    repo = Repository()
    # Always print to stdout too — useful when running on-demand
    print(build_briefing(repo))
    send(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
