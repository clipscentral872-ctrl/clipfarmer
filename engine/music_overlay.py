"""Overlay background music onto a finished clip.

Director's brief outputs `music_genre` + `music_energy` per campaign.
This module picks a matching music file from `data/music/` and mixes it
under the clip's dialogue audio at -20dB so it adds atmosphere without
drowning out speech.

Library layout:

    data/music/
        uplifting_electronic/
            high/   *.mp3
            mid/    *.mp3
            low/    *.mp3
        cinematic_orchestral/
            high/   *.mp3
            ...
        lo-fi_hip_hop/
            ...

Picks a random file from the (genre, energy) folder. If the folder
doesn't exist (genre mismatch), tries the genre's other energy levels,
then any music at all. If `data/music/` is empty, skips silently — the
clip is returned unchanged.

This means the system runs fine without a music library, and lights up
automatically once Chris drops files in.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


MUSIC_DIR = settings.project_root / "data" / "music"
# Music sits this far under the dialogue. -20dB ≈ subtle bed; -15dB
# is more present but risks overpowering quieter speakers.
MUSIC_DB_BELOW_VOICE = -20
# Fade in/out for cleaner edges
FADE_SEC = 0.8


def overlay_music(
    in_path: Path,
    out_path: Path,
    genre: str,
    energy: str,
    *,
    music_db: int = MUSIC_DB_BELOW_VOICE,
) -> Optional[Path]:
    """Return out_path with music overlaid, or None if no suitable file."""
    if not in_path.exists():
        raise FileNotFoundError(f"input clip missing: {in_path}")
    music_path = _pick_music(genre, energy)
    if not music_path:
        logger.info(
            f"[music] no library file matched genre={genre!r}/energy={energy!r}; "
            f"returning clip unchanged"
        )
        return None

    # Get clip duration so we can trim/fade the music to match.
    dur = _ffprobe_duration(in_path)
    if not dur or dur <= 0:
        return None
    fade_out_start = max(0.0, dur - FADE_SEC)

    # Build filter chain: scale music to -20dB, fade in/out, mix with original.
    # amerge is sometimes finicky; amix is more reliable.
    filter_complex = (
        f"[1:a]volume={music_db}dB,"
        f"afade=t=in:st=0:d={FADE_SEC},"
        f"afade=t=out:st={fade_out_start:.2f}:d={FADE_SEC}[m];"
        f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    cmd = [
        settings.ffmpeg_path or "ffmpeg", "-y",
        "-i", str(in_path),
        "-stream_loop", "-1", "-i", str(music_path),  # loop music if shorter
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",  # don't re-encode video
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out_path),
    ]
    logger.info(f"[music] {in_path.name} + {music_path.name} → {out_path.name}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        logger.warning(f"[music] ffmpeg failed: {r.stderr[-600:]}")
        return None
    return out_path


# ----------------------------------------------------------------------
def _pick_music(genre: str, energy: str) -> Optional[Path]:
    """Find a music file matching (genre, energy). Falls back gracefully."""
    if not MUSIC_DIR.exists():
        return None
    genre_slug = _slug(genre)
    energy_slug = _slug(energy)

    candidates: list[Path] = []
    # 1. Exact match
    exact_dir = MUSIC_DIR / genre_slug / energy_slug
    if exact_dir.exists():
        candidates = _files_in(exact_dir)
    # 2. Same genre, any energy
    if not candidates:
        gdir = MUSIC_DIR / genre_slug
        if gdir.exists():
            for child in gdir.iterdir():
                if child.is_dir():
                    candidates.extend(_files_in(child))
    # 3. Any music at all (last resort — better than nothing)
    if not candidates:
        for f in MUSIC_DIR.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg"):
                candidates.append(f)
    if not candidates:
        return None
    return random.choice(candidates)


def _files_in(d: Path) -> list[Path]:
    return [f for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg")]


def _slug(s: str) -> str:
    s = (s or "").lower().strip()
    # "uplifting electronic" → "uplifting_electronic"
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _ffprobe_duration(p: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            [settings.ffprobe_path or "ffprobe", "-loglevel", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


# ----------------------------------------------------------------------
# Convenience for the formatter / orchestrator: try to overlay music
# based on a campaign's creative brief. Returns the new path on success,
# or the original path if no music applied.
# ----------------------------------------------------------------------
def maybe_overlay_for_brief(
    clip_path: Path,
    brief: Optional[dict],
) -> Path:
    if not brief:
        return clip_path
    genre = brief.get("music_genre") or ""
    energy = brief.get("music_energy") or "mid"
    # "none" / "no music" / empty → skip
    if not genre or genre.lower() in ("none", "no music", "silence", "no"):
        return clip_path
    out = clip_path.with_name(clip_path.stem + "__music" + clip_path.suffix)
    result = overlay_music(clip_path, out, genre, energy)
    return result if result else clip_path
