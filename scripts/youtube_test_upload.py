"""Upload one of the cached test clips to YouTube as UNLISTED for a
sanity check. Prints the resulting URL.

Usage:
    python scripts/youtube_test_upload.py [clip_path]

If no clip is supplied, picks the first __final.mp4 in data/clips/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config import settings
from publisher.youtube_api import YouTubeAPIPublisher


def main() -> int:
    if len(sys.argv) > 1:
        clip = Path(sys.argv[1])
    else:
        candidates = sorted(settings.clips_dir.glob("*__final.mp4"))
        if not candidates:
            print("No __final.mp4 clips found under data/clips/. Run the engine first.")
            return 2
        clip = candidates[0]
    print(f"Uploading: {clip}")

    pub = YouTubeAPIPublisher()
    service = pub._get_service()

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": "clipfarmer test upload — please ignore",
            "description": "Sanity-check upload from the clipfarmer pipeline.\n#Shorts",
            "tags": ["test", "clipfarmer"],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "unlisted",   # <-- safe for testing
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(clip), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  upload {int(status.progress() * 100)}%")
    video_id = response.get("id", "")
    print(f"\nDONE — https://www.youtube.com/shorts/{video_id}")
    print("(unlisted — only visible to people with this link)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
