"""Auto-select the 9:16 layout based on subject count in the clip.

Rule:
  - 1 person on-screen → focus crop on them ("smart")
  - 2+ people (group shot) → preserve the full frame ("blur_pad")

Decision is made by sampling 3 evenly-spaced frames and asking Claude
vision how many people are clearly visible / talking. We take the max
across the samples so a 1-on-1 interview that briefly cuts to b-roll of
the host counts as 2-person (the conversation framing).

Falls back to "blur_pad" if vision is disabled / API unavailable —
strictly more conservative than wrongly cropping out a group.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


# How many people we count as a "group" — anything 2+ uses wide framing.
GROUP_THRESHOLD = 2


def decide_format_mode(
    video_path: Path,
    sample_count: int = 3,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Return 'smart' (single subject → crop) or 'group_focus' (group → tight wide).

    Both modes are optimized for short-form platform posting:
      - Subjects positioned in the upper-middle of the 9:16 frame so
        bottom UI (caption, like/comment rail, username) doesn't cover
        them.
      - For groups, we crop the source to the group's bounding box +
        margin, then 9:16-ify with blur padding — so the group appears
        as large as possible instead of shrinking to fit raw 16:9 inside
        9:16.
    """
    n = count_subjects(video_path, sample_count=sample_count, api_key=api_key, model=model)
    if n is None:
        logger.info("[auto-frame] subject count unavailable; defaulting to group_focus (safe wide)")
        return "group_focus"
    if n >= GROUP_THRESHOLD:
        logger.info(f"[auto-frame] {n} subjects → group_focus (tight wide, subjects upper-middle)")
        return "group_focus"
    logger.info(f"[auto-frame] {n} subject → single person → smart (focus crop, upper-middle)")
    return "smart"


def count_subjects(
    video_path: Path,
    sample_count: int = 3,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[int]:
    """Sample frames, ask Claude how many people, return max across samples."""
    api_key = api_key or settings.anthropic_api_key
    model = model or settings.anthropic_model
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    # Get duration so we can space samples evenly.
    dur = _ffprobe_duration(video_path)
    if not dur or dur <= 0:
        return None
    timestamps = [dur * (i + 1) / (sample_count + 1) for i in range(sample_count)]

    client = anthropic.Anthropic(api_key=api_key)
    counts: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(timestamps):
            frame_path = Path(tmp) / f"frame_{i}.jpg"
            if not _extract_frame(video_path, ts, frame_path):
                continue
            n = _count_in_frame(client, model, frame_path)
            if n is not None:
                counts.append(n)

    if not counts:
        return None
    # Take the max — if any moment shows a group, treat the clip as a group shot.
    return max(counts)


# ----------------------------------------------------------------------
_COUNT_PROMPT = (
    "How many distinct people are clearly visible in this image? "
    "Count people who are at least partially visible from the shoulders up — "
    "ignore tiny background figures or crowds in the distance. "
    "Return ONLY a JSON object: {\"count\": <integer>}"
)


def _count_in_frame(client, model: str, frame_path: Path) -> Optional[int]:
    try:
        with open(frame_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode("ascii")
        resp = client.messages.create(
            model=model,
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": img_b64,
                    }},
                    {"type": "text", "text": _COUNT_PROMPT},
                ],
            }],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        obj = json.loads(cleaned)
        n = int(obj.get("count", 0))
        return max(0, n)
    except Exception as e:
        logger.warning(f"[auto-frame] count failed for {frame_path.name}: {e}")
        return None


_GROUP_BBOX_PROMPT = (
    "Identify the bounding box that tightly contains ALL the people who are "
    "the subjects of this image (the speakers / focal figures). "
    "Return ONLY a JSON object: "
    '{"x": <left fraction 0..1>, "y": <top fraction 0..1>, '
    '"w": <width fraction 0..1>, "h": <height fraction 0..1>, '
    '"n_subjects": <int>}'
)


def decide_group_bbox(
    video_path: Path,
    sample_count: int = 3,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Return the union bounding box across sampled frames as
    {x, y, w, h} in source-image fractions. None if not determinable."""
    api_key = api_key or settings.anthropic_api_key
    model = model or settings.anthropic_model
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    dur = _ffprobe_duration(video_path)
    if not dur or dur <= 0:
        return None
    timestamps = [dur * (i + 1) / (sample_count + 1) for i in range(sample_count)]
    client = anthropic.Anthropic(api_key=api_key)

    boxes: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(timestamps):
            frame_path = Path(tmp) / f"frame_{i}.jpg"
            if not _extract_frame(video_path, ts, frame_path):
                continue
            box = _bbox_in_frame(client, model, frame_path)
            if box:
                boxes.append(box)

    if not boxes:
        return None
    # Union of all bboxes — covers anyone who appears in any sampled frame.
    x = min(b["x"] for b in boxes)
    y = min(b["y"] for b in boxes)
    right = max(b["x"] + b["w"] for b in boxes)
    bottom = max(b["y"] + b["h"] for b in boxes)
    return {"x": x, "y": y, "w": right - x, "h": bottom - y}


def _bbox_in_frame(client, model: str, frame_path: Path) -> Optional[dict]:
    try:
        with open(frame_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode("ascii")
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": img_b64,
                    }},
                    {"type": "text", "text": _GROUP_BBOX_PROMPT},
                ],
            }],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        obj = json.loads(cleaned)
        x = max(0.0, min(1.0, float(obj.get("x", 0))))
        y = max(0.0, min(1.0, float(obj.get("y", 0))))
        w = max(0.0, min(1.0, float(obj.get("w", 1))))
        h = max(0.0, min(1.0, float(obj.get("h", 1))))
        if w <= 0 or h <= 0:
            return None
        return {"x": x, "y": y, "w": w, "h": h}
    except Exception as e:
        logger.warning(f"[auto-frame] bbox failed for {frame_path.name}: {e}")
        return None


def _ffprobe_duration(video_path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            [settings.ffprobe_path, "-loglevel", "error",
             "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def _extract_frame(video_path: Path, ts_sec: float, out_path: Path) -> bool:
    try:
        r = subprocess.run(
            [settings.ffmpeg_path, "-loglevel", "error", "-y",
             "-ss", str(ts_sec), "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(out_path)],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0 and out_path.exists()
    except Exception:
        return False
