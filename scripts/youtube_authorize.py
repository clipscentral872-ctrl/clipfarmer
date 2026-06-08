"""One-shot YouTube OAuth authorization.

Opens a browser for the user to grant clipfarmer permission to upload
videos. Caches the resulting token to YOUTUBE_TOKEN_PATH. Does NOT
upload anything — just establishes the credential.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from publisher.youtube_api import YouTubeAPIPublisher


def main() -> int:
    pub = YouTubeAPIPublisher()
    pub._get_service()
    print("\nYouTube OAuth complete. Token cached. Future uploads will be unattended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
