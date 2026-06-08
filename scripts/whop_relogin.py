"""Fresh Whop login. Clears the cached session, opens a headed browser,
waits for the user to complete login (incl. 2FA), then caches the new
storage_state."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from scanner.whop_login import AUTH_FILE, WhopSession


def main() -> int:
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()
        logger.info(f"deleted cached session at {AUTH_FILE}")
    pub = WhopSession(headless=False)
    try:
        pub.start()
        logger.info("whop session saved")
        return 0
    finally:
        pub.close()


if __name__ == "__main__":
    sys.exit(main())
