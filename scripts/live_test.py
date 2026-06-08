"""Live publishing test — bypass scanner+engine, take an existing cached
clip, send to Telegram for approval, then post to YouTube + Instagram.

Usage:
    python scripts/live_test.py [clip_path]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config import settings
from publisher import (
    ApprovalStatus,
    InstagramGraphPublisher,
    TelegramGate,
    YouTubeAPIPublisher,
)


def main() -> int:
    if len(sys.argv) > 1:
        clip = Path(sys.argv[1])
    else:
        candidates = sorted(settings.clips_dir.glob("*__final.mp4"))
        if not candidates:
            print("No cached clips found. Run the engine first.")
            return 2
        clip = candidates[0]
    print(f"Using clip: {clip}")

    caption = "Quick test — change how people hear what you say."
    hashtags = ["Shorts", "Speaking", "Communication"]
    hook = "Words that change how people hear you"

    gate = TelegramGate()
    if not gate.enabled:
        print("Telegram gate is disabled (missing token or chat id).")
        return 2

    print("Sending to Telegram for approval...")
    gate.send_clip_for_approval(
        video_path=clip,
        campaign_title="clipfarmer live test",
        campaign_payout=None,
        hook_text=hook,
        caption_text=caption,
        hashtags=hashtags,
        platforms=["youtube", "instagram"],
    )
    print("Waiting for /approve or /reject in Telegram (up to 30 min)...")
    verdict = gate.wait_for_verdict(token="", timeout_minutes=30)
    if verdict.status != ApprovalStatus.APPROVED:
        print(f"Not approved ({verdict.status.value}). Aborting.")
        gate.notify(f"Live test not approved: {verdict.status.value}")
        return 0

    # --- YouTube --------------------------------------------------------
    print("\nPosting to YouTube (unlisted)...")
    try:
        from googleapiclient.http import MediaFileUpload
        ytp = YouTubeAPIPublisher()
        service = ytp._get_service()
        body = {
            "snippet": {
                "title": "clipfarmer live-test (Telegram approved)",
                "description": caption + "\n\n" + " ".join("#" + h for h in hashtags),
                "tags": [h for h in hashtags],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "unlisted",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(clip), chunksize=-1, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
        yt_id = response.get("id", "")
        yt_url = f"https://www.youtube.com/shorts/{yt_id}"
        print(f"  YouTube ok: {yt_url}")
        gate.notify(f"Posted to YouTube: {yt_url}")
    except Exception as e:
        logger.exception(f"YouTube post failed: {e}")
        gate.notify(f"YouTube post FAILED: {e}")

    # --- Instagram ------------------------------------------------------
    print("\nPosting to Instagram...")
    try:
        igp = InstagramGraphPublisher()
        result = igp.upload(clip, caption, hashtags)
        print(f"  Instagram ok: {result.post_url or result.platform_post_id}")
        gate.notify(f"Posted to Instagram: {result.post_url or result.platform_post_id}")
    except Exception as e:
        logger.exception(f"Instagram post failed: {e}")
        gate.notify(f"Instagram post FAILED: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
