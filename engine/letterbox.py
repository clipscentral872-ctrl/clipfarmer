"""Detect baked-in letterbox / pillarbox bars in a source video.

Cinematic food / film content is often uploaded with hardcoded black bars
top + bottom (e.g. 2.35:1 framed inside a 16:9 YouTube upload). When we
then crop to 9:16 we faithfully preserve those bars, which is the wrong
behaviour — we want to crop to the actual CONTENT, not to the framing.

This module uses ffmpeg's built-in `cropdetect` filter to find the
inner content rectangle, then returns it for the formatter to use as a
pre-crop stage.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


@dataclass
class ContentBox:
    w: int
    h: int
    x: int
    y: int
    source_w: int
    source_h: int

    @property
    def is_trivial(self) -> bool:
        """True when the detected box equals the full source — no bars present."""
        return (
            self.w == self.source_w
            and self.h == self.source_h
            and self.x == 0
            and self.y == 0
        )

    @property
    def ffmpeg_crop(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


def detect_content_box(
    video_path: Path,
    sample_seconds: int = 20,
    threshold: int = 24,
) -> Optional[ContentBox]:
    """Run ffmpeg cropdetect on a chunk of the source and return the box.

    `sample_seconds` controls how much footage to scan (more = more accurate,
    a bit slower). `threshold` is cropdetect's black-level cutoff (16-32 is
    typical for video; 24 is a safe default).
    """
    if not video_path.exists():
        return None

    ffprobe = settings.ffprobe_path or "ffprobe"
    ffmpeg = settings.ffmpeg_path or "ffmpeg"

    # First get the actual source dimensions so we can tell if the detected
    # crop is non-trivial.
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        parts = r.stdout.strip().split(",")
        source_w, source_h = int(parts[0]), int(parts[1])
    except Exception as e:
        logger.warning(f"[letterbox] ffprobe dims failed: {e}")
        return None

    # Run cropdetect over a middle slice of the video (skips intros).
    try:
        r2 = subprocess.run(
            [
                ffmpeg, "-hide_banner",
                "-ss", "30",                # skip the first 30s (intros / titles)
                "-i", str(video_path),
                "-t", str(sample_seconds),
                "-vf", f"cropdetect={threshold}:16:0",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        logger.warning(f"[letterbox] ffmpeg cropdetect failed: {e}")
        return None

    # cropdetect logs to stderr — find the LAST crop= line it printed (most stable estimate).
    matches = _CROP_RE.findall(r2.stderr or "")
    if not matches:
        logger.info("[letterbox] cropdetect produced no crop= lines; assuming no bars")
        return ContentBox(source_w, source_h, 0, 0, source_w, source_h)

    w, h, x, y = (int(v) for v in matches[-1])
    # Round to even (codecs require even dims).
    w -= w % 2
    h -= h % 2
    box = ContentBox(w=w, h=h, x=x, y=y, source_w=source_w, source_h=source_h)

    if box.is_trivial:
        logger.info(f"[letterbox] no letterbox detected ({source_w}x{source_h})")
    else:
        trim_pct = 100.0 * (1.0 - (box.w * box.h) / (source_w * source_h))
        logger.info(
            f"[letterbox] content {box.w}x{box.h} at ({box.x},{box.y}) "
            f"inside {source_w}x{source_h} — trimming {trim_pct:.1f}% of bars"
        )
    return box
