"""Cobalt API-based YouTube downloader.

yt-dlp on GitHub Actions IPs hits YouTube's SABR force-routing as of mid-2025
and gets "Requested format is not available" on every video, even with
cookies + the smart-TV player client.  Cobalt (https://cobalt.tools) is a
community-run service that handles the SABR + PO Token negotiation
server-side and returns a normal download URL we can fetch with a plain
HTTP client.

Public instances we cycle through if one is down or rate-limiting:
  - https://co.wuk.sh                 (community)
  - https://cobalt-api.kwiatekmiki.com (community)
  - https://api.cobalt.tools          (official, often token-gated)

If every instance fails we re-raise so the caller can decide whether to
fall back to yt-dlp.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import requests
from loguru import logger


COBALT_INSTANCES = (
    "https://co.wuk.sh",
    "https://cobalt-api.kwiatekmiki.com",
    "https://api.cobalt.tools",
)


class CobaltDownloadError(RuntimeError):
    pass


class PytubefixDownloadError(RuntimeError):
    pass


def download_youtube_pytubefix(source_url: str, output_path: Path) -> Path:
    """Try pytubefix — independent of yt-dlp, sometimes works on cloud IPs
    where yt-dlp is SABR-blocked.  Picks the highest-resolution progressive
    stream so we get one file with audio baked in (no merge needed)."""
    try:
        from pytubefix import YouTube
    except ImportError as e:
        raise PytubefixDownloadError("pytubefix not installed") from e

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        yt = YouTube(source_url)
        # progressive=True streams contain both audio+video in one file
        # (capped at 720p) — simpler than the adaptive merge dance.
        stream = (
            yt.streams.filter(progressive=True, file_extension="mp4")
            .order_by("resolution")
            .desc()
            .first()
        )
        if stream is None:
            # Fall back to highest-res adaptive video (no audio) — better
            # than nothing; ffmpeg downstream can still cut from it.
            stream = (
                yt.streams.filter(adaptive=True, file_extension="mp4", only_video=True)
                .order_by("resolution")
                .desc()
                .first()
            )
        if stream is None:
            raise PytubefixDownloadError("no usable stream found")
        downloaded = stream.download(
            output_path=str(output_path.parent),
            filename=output_path.name,
            skip_existing=False,
        )
        logger.info(f"[pytubefix] saved {output_path.name} ({Path(downloaded).stat().st_size:,} bytes)")
        return Path(downloaded)
    except Exception as e:
        raise PytubefixDownloadError(f"pytubefix failed: {e}") from e


def download_youtube(source_url: str, output_path: Path, timeout: int = 600) -> Path:
    """Download a YouTube video via Cobalt.  Returns the output Path on success.
    Raises CobaltDownloadError if every instance fails.

    `output_path` is the .mp4 we want to land at; we use it for the local
    streamed download.  Cobalt picks the best mp4/audio combo automatically."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    last_err: Optional[Exception] = None
    for instance in COBALT_INSTANCES:
        try:
            stream_url = _resolve_stream_url(instance, source_url)
        except Exception as e:
            logger.warning(f"[cobalt] {instance} resolve failed: {e}")
            last_err = e
            continue

        try:
            _stream_to_file(stream_url, output_path, timeout=timeout)
            size = output_path.stat().st_size
            logger.info(f"[cobalt] saved {output_path.name} ({size:,} bytes) via {instance}")
            return output_path
        except Exception as e:
            logger.warning(f"[cobalt] {instance} stream failed: {e}")
            last_err = e
            try:
                if output_path.exists():
                    output_path.unlink()
            except Exception:
                pass
            continue

    raise CobaltDownloadError(f"every Cobalt instance failed; last error: {last_err!r}")


def _resolve_stream_url(instance: str, source_url: str) -> str:
    """POST to a Cobalt instance and return the actual download URL it gives us."""
    body = {
        "url": source_url,
        "videoQuality": "1080",
        "audioFormat": "mp3",
        "downloadMode": "auto",
        "filenameStyle": "basic",
    }
    r = requests.post(
        instance,
        json=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=60,
    )
    if r.status_code != 200:
        raise CobaltDownloadError(f"{instance} HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise CobaltDownloadError(f"{instance} returned non-JSON: {r.text[:200]}") from e

    status = (data.get("status") or "").lower()
    if status in ("redirect", "tunnel"):
        return data["url"]
    if status == "picker":
        # Multi-stream picker (album, image picker, etc.) — first item is fine.
        items = data.get("picker") or []
        if not items:
            raise CobaltDownloadError(f"{instance} picker with no items")
        return items[0].get("url") or items[0].get("thumb")
    if status == "error":
        text = (data.get("error") or {}).get("code") or data
        raise CobaltDownloadError(f"{instance} error: {text}")
    raise CobaltDownloadError(f"{instance} unknown status: {status!r}")


def _stream_to_file(stream_url: str, output_path: Path, timeout: int) -> None:
    start = time.time()
    with requests.get(stream_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with output_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if time.time() - start > timeout:
                    raise CobaltDownloadError("stream exceeded timeout")
                if chunk:
                    f.write(chunk)
