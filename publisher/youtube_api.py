"""Post a Short to YouTube via the official YouTube Data API v3.

Uses OAuth 2.0 (installed-app flow). First run opens a browser for the
user to grant permission; the refresh token is cached at
YOUTUBE_TOKEN_PATH so subsequent runs are unattended.

Credentials needed:
  - YOUTUBE_CLIENT_SECRET_PATH — path to client_secret_<id>.json from
    Google Cloud Console (APIs & Services → Credentials → OAuth client
    ID → Desktop app type).
  - YOUTUBE_TOKEN_PATH — where we cache the OAuth token (any writable
    path, e.g. `./.auth/youtube-token.json`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings

from .base import PublishResult


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only access for analytics on our own uploaded videos.
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class YouTubeAPIError(RuntimeError):
    pass


class YouTubeAPIPublisher:
    platform = "youtube"

    def __init__(
        self,
        client_secret_path: Optional[str] = None,
        token_path: Optional[str] = None,
    ) -> None:
        self.client_secret_path = client_secret_path or settings.youtube_client_secret_path
        self.token_path = token_path or settings.youtube_token_path or str(
            settings.project_root / ".auth" / "youtube-token.json"
        )
        if not self.client_secret_path:
            raise YouTubeAPIError(
                "YOUTUBE_CLIENT_SECRET_PATH missing — download client_secret JSON from Google Cloud Console"
            )
        if not Path(self.client_secret_path).exists():
            raise YouTubeAPIError(f"client secret file not found: {self.client_secret_path}")
        self._service = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        pass

    # ------------------------------------------------------------------
    def _get_service(self):
        if self._service is not None:
            return self._service
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None
        token_p = Path(self.token_path)
        if token_p.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_p), SCOPES)
            except Exception as e:
                logger.warning(f"[youtube] could not load cached token: {e}")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("[youtube] refreshing OAuth token")
                creds.refresh(Request())
            else:
                logger.info("[youtube] running OAuth installed-app flow (browser will open)")
                flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_path, SCOPES)
                creds = flow.run_local_server(port=0)
            token_p.parent.mkdir(parents=True, exist_ok=True)
            token_p.write_text(creds.to_json(), encoding="utf-8")
            logger.info(f"[youtube] cached token to {token_p}")

        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # ------------------------------------------------------------------
    def upload(self, video_path: Path, caption: str, hashtags: list[str]) -> PublishResult:
        from googleapiclient.http import MediaFileUpload

        if not video_path.exists():
            raise YouTubeAPIError(f"video not found: {video_path}")
        service = self._get_service()

        title = caption.split("\n", 1)[0][:95] or video_path.stem
        # Make sure it gets the #Shorts treatment (must include #Shorts in title or description).
        full_caption = caption.rstrip()
        if hashtags:
            full_caption += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)
        if "#Shorts" not in full_caption and "#shorts" not in full_caption:
            full_caption += "\n#Shorts"

        body = {
            "snippet": {
                "title": title,
                "description": full_caption,
                "tags": [h.lstrip("#") for h in hashtags][:20],
                "categoryId": "22",  # People & Blogs (broad default)
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
        logger.info(f"[youtube] uploading {video_path.name}")
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"[youtube] upload progress {int(status.progress() * 100)}%")
        video_id = response.get("id", "")
        if not video_id:
            raise YouTubeAPIError(f"upload returned no video id: {response}")
        post_url = f"https://www.youtube.com/shorts/{video_id}"
        logger.info(f"[youtube] posted: {post_url}")
        return PublishResult(
            platform="youtube",
            platform_post_id=video_id,
            post_url=post_url,
            metricool_post_id="",
            scheduled_for=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw=response,
        )
