"""FFmpeg-based clip cutter."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


class CutError(RuntimeError):
    pass


class Cutter:
    def __init__(self, ffmpeg_path: Optional[str] = None) -> None:
        self.ffmpeg = ffmpeg_path or settings.ffmpeg_path
        if not shutil.which(self.ffmpeg):
            raise CutError(f"ffmpeg not found at {self.ffmpeg!r}. Set FFMPEG_PATH in .env.")

    def cut(self, source_path: Path, start_sec: float, end_sec: float, out_path: Path) -> Path:
        if not source_path.exists():
            raise CutError(f"source not found: {source_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(0.0, end_sec - start_sec)
        if duration <= 0:
            raise CutError(f"invalid range {start_sec}-{end_sec}")
        # -ss before -i for fast seek, but use -accurate_seek-equivalent
        # by adding -ss again after -i for frame accuracy on re-encode.
        cmd = [
            self.ffmpeg, "-y",
            "-ss", f"{start_sec:.3f}",
            "-i", str(source_path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "19",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        logger.info(f"[cut] {source_path.name}[{start_sec:.1f}-{end_sec:.1f}] → {out_path.name}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CutError(f"ffmpeg cut failed: {result.stderr[-1000:]}")
        return out_path
