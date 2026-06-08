"""Download a source video with yt-dlp.

Designed to be robust for YouTube, TikTok, Instagram, X/Twitter, Twitch,
Kick, Vimeo, Rumble — anything yt-dlp supports.

Drops the file at data/downloads/<source_id>.<ext>.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


class DownloadError(RuntimeError):
    pass


class Downloader:
    """Thin wrapper around yt-dlp's Python API."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        self.output_dir = output_dir or settings.download_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def download(self, source_url: str, prefer_mp4: bool = True) -> Path:
        """Download source_url. Returns the local path of the downloaded file.

        Accepts:
          - An http(s) URL yt-dlp supports
          - A `file://...` URL
          - A bare absolute path to an existing local video

        Routing:
          - file paths      → return as-is
          - youtube urls    → Cobalt API first (yt-dlp + SABR is broken on
                              cloud IPs as of mid-2025); yt-dlp fallback
          - everything else → yt-dlp
        """
        local = _resolve_local_path(source_url)
        if local is not None:
            logger.info(f"[download] using local file {local}")
            return local

        # YouTube downloads go through Cobalt first.  yt-dlp on cloud-IP
        # GitHub Actions runners hits YouTube's SABR force-routing and gets
        # "Requested format is not available" on every video — Cobalt
        # handles that server-side.  If Cobalt is down we fall through.
        url_lower = (source_url or "").lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            try:
                from engine.cobalt_downloader import download_youtube, CobaltDownloadError
                source_id = self._stable_id(source_url)
                out_path = self.output_dir / f"{source_id}.mp4"
                return download_youtube(source_url, out_path)
            except CobaltDownloadError as e:
                logger.warning(f"[download] Cobalt failed ({e}); trying yt-dlp")
            except Exception as e:
                logger.warning(f"[download] Cobalt errored ({e}); trying yt-dlp")

        try:
            from yt_dlp import YoutubeDL
        except ImportError as e:
            raise DownloadError("yt-dlp not installed") from e

        source_id = self._stable_id(source_url)
        outtmpl = str(self.output_dir / f"{source_id}.%(ext)s")

        # Format selection: take ANY best video + best audio, let yt-dlp
        # remux to mp4 via merge_output_format below.  The strict
        # h264-only filter we used to have breaks on modern YouTube videos
        # served only as VP9/AV1 — by 2025 that's the majority.
        fmt = "bv*+ba/best" if prefer_mp4 else "best"

        # yt-dlp shells out to ffmpeg to merge audio+video; it only looks
        # at PATH unless we point it explicitly. Use the parent directory
        # of our configured ffmpeg binary.
        ffmpeg_location = str(Path(settings.ffmpeg_path).parent) if Path(settings.ffmpeg_path).is_absolute() else None

        opts = {
            "outtmpl": outtmpl,
            "format": fmt,
            "merge_output_format": "mp4",
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            **({"ffmpeg_location": ffmpeg_location} if ffmpeg_location else {}),
            # Be a polite client.
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0.0.0 Safari/537.36"
                )
            },
            # YouTube on datacenter IPs (GitHub Actions runners) gets the
            # "Sign in to confirm you're not a bot" challenge.  Pass cookies
            # exported from Chris's signed-in browser if available.
            **_youtube_cookie_opts(source_url),
            # Even with cookies, the YouTube *web* player JS challenge can
            # fail on cloud runners and produce "Requested format is not
            # available" because format extraction never returns anything.
            # The android/ios player clients return formats directly without
            # the n-sig challenge, so prefer them for cloud-based downloads.
            **_youtube_extractor_args(source_url),
        }

        logger.info(f"[download] {source_url} → {source_id}.*")
        with YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(source_url, download=True)
            except Exception as e:
                raise DownloadError(f"yt-dlp failed: {e}") from e

        if info is None:
            raise DownloadError("yt-dlp returned no info")
        # yt-dlp's "filename" is set after a successful download.
        path = info.get("requested_downloads", [{}])[0].get("filepath")
        if not path:
            # Best-effort: look for the file by id.
            candidates = sorted(self.output_dir.glob(f"{source_id}.*"))
            if candidates:
                path = str(candidates[0])
        if not path or not Path(path).exists():
            raise DownloadError("could not locate downloaded file")
        out_path = Path(path)
        logger.info(f"[download] saved {out_path} ({out_path.stat().st_size:,} bytes)")
        return out_path

    def _stable_id(self, source_url: str) -> str:  # noqa: D401
        return _stable_id(source_url)


def _resolve_local_path(source: str) -> Optional[Path]:
    """If `source` points at an existing local file, return its absolute Path."""
    s = source.strip()
    if s.lower().startswith("file:///"):
        s = s[len("file:///"):]
    elif s.lower().startswith("file://"):
        s = s[len("file://"):]
    # Windows paths after URL prefix can come back like "C:/foo.mp4" — accept.
    p = Path(s)
    try:
        if p.is_file():
            return p.resolve()
    except OSError:
        pass
    return None


def _stable_id(source_url: str) -> str:
    """A short deterministic id derived from the URL."""
    h = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:10]
    m = re.search(r"(?:v=|/shorts/|/watch/|/video/|/p/|/reel/|/clip/)([A-Za-z0-9_-]{6,})", source_url)
    if m:
        return f"{m.group(1)}_{h}"
    return f"src_{h}"


def _youtube_cookie_opts(source_url: str) -> dict:
    """Return `{"cookiefile": "<path>"}` when downloading from YouTube AND a
    cookies file is present on disk.  Other domains don't need the cookie
    because the bot-block is YouTube-specific.

    Setup: export YouTube cookies from a signed-in browser session (the
    `Get cookies.txt LOCALLY` Chrome/Firefox extension is the standard tool),
    save the file to `.auth/youtube-cookies.txt` in the project root, and
    commit it to the encrypted state branch via the next bootstrap.
    """
    url = (source_url or "").lower()
    if not ("youtube.com" in url or "youtu.be" in url):
        return {}
    cookies_path = settings.project_root / ".auth" / "youtube-cookies.txt"
    if cookies_path.exists():
        return {"cookiefile": str(cookies_path)}
    return {}


def _youtube_extractor_args(source_url: str) -> dict:
    """As of mid-2025 YouTube force-routes most clients into SABR streaming
    which yt-dlp cannot decode, producing "Requested format is not available"
    on every download attempt (issue yt-dlp/yt-dlp#12482).  The smart-TV
    client (`tv`) is the last reliable bypass — it returns regular DASH
    formats without the SABR challenge.  We keep tv_embedded as a fallback."""
    url = (source_url or "").lower()
    if not ("youtube.com" in url or "youtu.be" in url):
        return {}
    return {
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "tv_embedded"],
            },
        },
        # Verbose so the workflow log shows which formats yt-dlp actually
        # sees from each client — invaluable when SABR/PO Token changes
        # break things again (and they will).
        "verbose": True,
    }
