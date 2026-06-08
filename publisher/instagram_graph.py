"""Post a Reel to Instagram via the official Meta Graph API.

Reels flow (Meta docs):
  1. POST /{ig-user-id}/media with media_type=REELS, video_url=<public URL>
     OR with upload_type=resumable for direct binary upload.
  2. Poll /{container-id}?fields=status_code until FINISHED.
  3. POST /{ig-user-id}/media_publish with creation_id=<container-id>.

Credentials needed (.env):
  - INSTAGRAM_USER_ID — your Instagram Business/Creator account id
    (numeric). Get it via /me/accounts at Meta for Developers.
  - INSTAGRAM_ACCESS_TOKEN — long-lived user access token with
    instagram_basic + instagram_content_publish permissions.

This implementation uses the resumable upload route so we can upload
local mp4 files without standing up a public HTTP server.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config import settings

from .base import PublishResult


GRAPH_BASE = "https://graph.facebook.com/v21.0"
RUPLOAD_BASE = "https://rupload.facebook.com/ig-api-upload/v21.0"


class InstagramGraphError(RuntimeError):
    pass


class InstagramGraphPublisher:
    platform = "instagram"

    def __init__(
        self,
        ig_user_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self.ig_user_id = ig_user_id or settings.instagram_user_id
        self.access_token = access_token or settings.instagram_access_token
        if not self.ig_user_id or not self.access_token:
            raise InstagramGraphError(
                "INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN must be set in .env"
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        pass

    # ------------------------------------------------------------------
    def upload(self, video_path: Path, caption: str, hashtags: list[str]) -> PublishResult:
        if not video_path.exists():
            raise InstagramGraphError(f"video not found: {video_path}")

        full_caption = caption.rstrip()
        if hashtags:
            full_caption += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)

        # Step 1: start a resumable upload session.
        logger.info(f"[instagram] starting resumable upload for {video_path.name}")
        r = requests.post(
            f"{GRAPH_BASE}/{self.ig_user_id}/media",
            params={
                "access_token": self.access_token,
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": full_caption,
            },
            timeout=60,
        )
        if r.status_code >= 400:
            raise InstagramGraphError(f"create container failed: {r.status_code} {r.text}")
        data = r.json()
        container_id = data.get("id")
        upload_url = data.get("uri") or f"{RUPLOAD_BASE}/{container_id}"
        if not container_id:
            raise InstagramGraphError(f"no container id in response: {data}")

        # Step 2: upload the binary.
        # Meta's resumable Reels endpoint documents only three headers:
        # Authorization, offset, file_size. Adding extras like Content-Type or
        # an explicit Content-Length confuses their server-side processor and
        # returns the generic ProcessingFailedError. We read the file into
        # memory and let `requests` set Content-Length naturally from `data=`.
        file_size = video_path.stat().st_size
        body = video_path.read_bytes()
        r2 = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {self.access_token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=body,
            timeout=600,
        )
        if r2.status_code >= 400:
            raise InstagramGraphError(f"upload failed: {r2.status_code} {r2.text}")

        # Step 3: poll until container is finished.
        logger.info(f"[instagram] waiting for container {container_id} to finish")
        deadline = time.time() + 600
        while time.time() < deadline:
            r3 = requests.get(
                f"{GRAPH_BASE}/{container_id}",
                params={"fields": "status_code,status", "access_token": self.access_token},
                timeout=30,
            )
            if r3.status_code >= 400:
                raise InstagramGraphError(f"status check failed: {r3.status_code} {r3.text}")
            status = r3.json().get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise InstagramGraphError(f"container failed: {r3.json()}")
            time.sleep(5)
        else:
            raise InstagramGraphError("container did not finish in time")

        # Step 4: publish the container.
        logger.info("[instagram] publishing container")
        r4 = requests.post(
            f"{GRAPH_BASE}/{self.ig_user_id}/media_publish",
            params={"creation_id": container_id, "access_token": self.access_token},
            timeout=60,
        )
        if r4.status_code >= 400:
            raise InstagramGraphError(f"publish failed: {r4.status_code} {r4.text}")
        published = r4.json()
        post_id = published.get("id", "")
        post_url = ""
        if post_id:
            r5 = requests.get(
                f"{GRAPH_BASE}/{post_id}",
                params={"fields": "permalink", "access_token": self.access_token},
                timeout=30,
            )
            if r5.status_code < 400:
                post_url = r5.json().get("permalink", "")
        logger.info(f"[instagram] posted: {post_url or post_id}")
        return PublishResult(
            platform="instagram",
            platform_post_id=post_id,
            post_url=post_url,
            metricool_post_id="",
            scheduled_for=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw=published,
        )
