"""One-shot login flow for a platform publisher.

Usage:
    python scripts/platform_login.py tiktok
    python scripts/platform_login.py youtube
    python scripts/platform_login.py instagram

Opens a visible browser, waits for you to log in (up to 5 minutes),
then saves the session to .auth/<platform>.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from publisher.instagram_web import InstagramWebPublisher
from publisher.tiktok_web import TikTokWebPublisher
from publisher.youtube_web import YouTubeWebPublisher


PUBLISHERS = {
    "tiktok": TikTokWebPublisher,
    "youtube": YouTubeWebPublisher,
    "instagram": InstagramWebPublisher,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in PUBLISHERS:
        print(f"usage: python scripts/platform_login.py {{tiktok|youtube|instagram}}")
        return 2
    name = sys.argv[1]
    cls = PUBLISHERS[name]
    pub = cls()
    # Force headed so the user actually sees the login.
    pub.session.headless = False
    try:
        pub.session.start()
        logger.info(f"[{name}] session saved")
        return 0
    finally:
        pub.session.close()


if __name__ == "__main__":
    sys.exit(main())
