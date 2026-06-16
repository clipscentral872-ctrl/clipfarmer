"""Vision-guided crop: ask Claude where the speaker is and crop to them.

The haar-cascade `face_crop` works for clean 1-person or 2-person shots but
fails on 3-way podcast frames (Open Tab) and any unusual composition.

This module replaces the geometry guess with a Claude vision call:
  1. Sample 3 frames evenly across the clip.
  2. Ask Claude for the speaker's bounding box (head + shoulders) in each.
  3. Aggregate boxes (median) for stability.
  4. Compute a 9:16 crop window in source coordinates that comfortably
     contains the speaker, then scale to 1080x1920.

Falls back to None if vision is unavailable / disabled — the formatter then
hands off to the haar-cascade path so we don't break existing runs.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from config import settings


# ----------------------------------------------------------------------
@dataclass
class SpeakerCrop:
    """A 9:16 crop window in source pixel coordinates."""
    crop_w: int
    crop_h: int
    crop_x: int
    crop_y: int
    source_w: int
    source_h: int
    notes: str = ""

    @property
    def ffmpeg_vf(self) -> str:
        """ffmpeg -vf string for this crop, scaled to 1080x1920 9:16."""
        return (
            f"crop={self.crop_w}:{self.crop_h}:{self.crop_x}:{self.crop_y},"
            f"scale=1080:1920,setsar=1"
        )


# ----------------------------------------------------------------------
def decide_speaker_crop(
    video_path: Path,
    sample_count: int = 3,
    margin_factor: float = 1.8,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[SpeakerCrop]:
    """Sample frames, ask Claude vision for the speaker, return a 9:16 crop window.

    Returns None if anything fails — formatter will fall back to haar cascade.
    """
    if not video_path.exists():
        return None

    source_dims = _ffprobe_dimensions(video_path)
    if not source_dims:
        logger.warning("[vis-crop] could not read source dimensions")
        return None
    W, H = source_dims
    logger.info(f"[vis-crop] source {W}x{H}, sampling {sample_count} frame(s)")

    frames = _sample_frames(video_path, sample_count, width_px=720)
    if not frames:
        return None

    api_key = api_key or settings.anthropic_api_key
    if not api_key:
        logger.warning("[vis-crop] ANTHROPIC_API_KEY missing")
        return None
    model = model or settings.anthropic_model

    try:
        from engine import llm_compat as anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        logger.warning(f"[vis-crop] anthropic SDK unavailable: {e}")
        return None

    boxes: List[Tuple[float, float, float, float, str]] = []
    for frame in frames:
        bbox = _ask_claude_for_speaker_bbox(client, model, frame)
        if bbox is not None:
            boxes.append(bbox)
        try:
            frame.unlink()
        except Exception:
            pass

    if not boxes:
        logger.warning("[vis-crop] vision returned no usable boxes")
        return None

    # Aggregate: median of each coordinate.
    cxs = sorted(b[0] + b[2] / 2 for b in boxes)
    cys = sorted(b[1] + b[3] / 2 for b in boxes)
    bws = sorted(b[2] for b in boxes)
    bhs = sorted(b[3] for b in boxes)
    mid = len(boxes) // 2
    speaker_cx_frac = cxs[mid]
    speaker_cy_frac = cys[mid]
    speaker_bw_frac = bws[mid]
    speaker_bh_frac = bhs[mid]

    # Convert to source pixels.
    scx = speaker_cx_frac * W
    scy = speaker_cy_frac * H
    sbw = speaker_bw_frac * W
    sbh = speaker_bh_frac * H

    # Target 9:16 crop region containing the speaker with margin.
    target_ratio = 9.0 / 16.0  # width / height
    crop_h = min(H, sbh * margin_factor)
    crop_w = crop_h * target_ratio
    if crop_w > W:
        # Source is too narrow — clamp width and reduce height accordingly.
        crop_w = W
        crop_h = crop_w / target_ratio

    # Position crop centred on the speaker, clamped to source bounds.
    crop_x = max(0.0, min(W - crop_w, scx - crop_w / 2))
    crop_y = max(0.0, min(H - crop_h, scy - crop_h / 2))

    # Even integers (some codecs require even dimensions).
    crop_w_i = int(crop_w) - (int(crop_w) % 2)
    crop_h_i = int(crop_h) - (int(crop_h) % 2)
    crop_x_i = int(crop_x) - (int(crop_x) % 2)
    crop_y_i = int(crop_y) - (int(crop_y) % 2)

    note = f"speaker@({speaker_cx_frac:.2f},{speaker_cy_frac:.2f}) bbox={speaker_bw_frac:.2f}x{speaker_bh_frac:.2f}"
    logger.info(
        f"[vis-crop] crop={crop_w_i}x{crop_h_i} at ({crop_x_i},{crop_y_i}) — {note}"
    )
    return SpeakerCrop(
        crop_w=crop_w_i,
        crop_h=crop_h_i,
        crop_x=crop_x_i,
        crop_y=crop_y_i,
        source_w=W,
        source_h=H,
        notes=note,
    )


# ----------------------------------------------------------------------
def _ask_claude_for_speaker_bbox(
    client, model: str, image_path: Path,
) -> Optional[Tuple[float, float, float, float, str]]:
    """Return (x, y, width, height, notes) as 0..1 fractions, or None."""
    try:
        data = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    except Exception as e:
        logger.warning(f"[vis-crop] cannot read frame: {e}")
        return None

    prompt = (
        "You're looking at a frame from a video being clipped for vertical 9:16 short-form (TikTok / Reels / "
        "YouTube Shorts). Identify the FULL VISUAL SUBJECT — everything the viewer needs to see for this moment "
        "to make sense. Specifically:\n"
        "  - For a FOOD / DEMO / PRODUCT video: include BOTH the presenter AND the food / dish / product they "
        "    are showing or interacting with. The food/product is half the story — do NOT crop it out.\n"
        "  - For a PERFORMANCE / ACTION (concert, sport, stunt): include the performer plus the immediate "
        "    action they're performing.\n"
        "  - For a TALKING HEAD (interview, podcast, monologue with no visual prop): just include the "
        "    speaker's head + shoulders.\n\n"
        "Return ONLY JSON, no commentary, with this shape:\n"
        '{"x": 0.NNN, "y": 0.NNN, "width": 0.NNN, "height": 0.NNN, "notes": "brief"}\n\n'
        "Coordinates are fractions 0..1 of the image (0,0 = top-left, 1,1 = bottom-right). The box should "
        "GENEROUSLY contain the full visual subject — err on the side of slightly TOO MUCH context rather than "
        "TOO LITTLE. Better to keep extra background than to crop out the food / hands / props the presenter is "
        "showing.\n\n"
        "If you cannot identify any subject, return "
        '{"x":0,"y":0,"width":1,"height":1,"notes":"no clear subject"}.'
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": prompt},
    ]
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        logger.warning(f"[vis-crop] Claude vision call failed: {e}")
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0:
        logger.warning(f"[vis-crop] no JSON in vision response: {text[:200]}")
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        x = float(obj["x"])
        y = float(obj["y"])
        w = float(obj["width"])
        h = float(obj["height"])
        notes = str(obj.get("notes", ""))[:200]
    except Exception as e:
        logger.warning(f"[vis-crop] parse failed: {e}")
        return None

    # Clamp & sanity-check.
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.05, min(1.0, w))
    h = max(0.05, min(1.0, h))
    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y
    return (x, y, w, h, notes)


# ----------------------------------------------------------------------
def _ffprobe_dimensions(video_path: Path) -> Optional[Tuple[int, int]]:
    ffprobe = settings.ffprobe_path or "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        parts = r.stdout.strip().split(",")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception as e:
        logger.warning(f"[vis-crop] ffprobe dims failed: {e}")
    return None


def _sample_frames(video_path: Path, count: int, width_px: int) -> List[Path]:
    ffprobe = settings.ffprobe_path or "ffprobe"
    ffmpeg = settings.ffmpeg_path or "ffmpeg"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip())
    except Exception as e:
        logger.warning(f"[vis-crop] ffprobe duration failed: {e}")
        return []
    if duration <= 0:
        return []

    tmpdir = Path(tempfile.mkdtemp(prefix="clipfarmer_viscrop_"))
    out: List[Path] = []
    for i in range(count):
        t = duration * (i + 1) / (count + 1)
        out_path = tmpdir / f"vframe_{i:02d}.jpg"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{t:.2f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale={width_px}:-2",
            "-q:v", "3",
            str(out_path),
        ]
        r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r2.returncode == 0 and out_path.exists():
            out.append(out_path)
    return out
