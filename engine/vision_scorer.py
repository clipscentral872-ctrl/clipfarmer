"""Visual scoring layer.

The transcript-only scorer can't see what's actually happening on screen —
a stunt, a reaction shot, a fight breaking out, a dramatic facial expression.
This module samples a few frames from each candidate moment, sends them to
Claude with vision, and returns a 0-100 visual score that the main scorer
blends with its transcript score.

Designed to be cheap: 3 frames per candidate, scaled to ~640px wide before
base64 encoding so the prompt stays under a few hundred KB per call.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings


@dataclass
class VisionAssessment:
    visual_score: float          # 0..100
    key_visual: str              # one-sentence description
    has_strong_reaction: bool


class VisionScorer:
    def __init__(
        self,
        anthropic_api_key: Optional[str] = None,
        model: Optional[str] = None,
        frames_per_moment: int = 3,
        frame_width_px: int = 640,
    ) -> None:
        self.api_key = anthropic_api_key or settings.anthropic_api_key
        self.model = model or settings.anthropic_model
        self.frames_per_moment = frames_per_moment
        self.frame_width_px = frame_width_px
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        from engine import llm_compat as anthropic
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # ------------------------------------------------------------------
    def assess(
        self,
        video_path: Path,
        start_sec: float,
        end_sec: float,
        transcript_excerpt: str,
    ) -> Optional[VisionAssessment]:
        """Sample frames from the moment window and ask Claude to rate the visual."""
        frames = _sample_frames_in_window(
            video_path, start_sec, end_sec,
            count=self.frames_per_moment,
            width_px=self.frame_width_px,
        )
        if not frames:
            logger.warning(f"[vision] no frames sampled for {start_sec:.1f}-{end_sec:.1f}")
            return None

        try:
            image_blocks = [_encode_image_block(p) for p in frames]
        finally:
            for p in frames:
                try:
                    p.unlink()
                except Exception:
                    pass

        text_block = {
            "type": "text",
            "text": _build_prompt(start_sec, end_sec, transcript_excerpt, len(image_blocks)),
        }
        content = image_blocks + [text_block]

        try:
            resp = self._get_client().messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:
            logger.warning(f"[vision] Claude vision call failed: {e}")
            return None

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse_assessment(text)


# ----------------------------------------------------------------------
def _build_prompt(start_sec: float, end_sec: float, transcript_excerpt: str, n_frames: int) -> str:
    excerpt = (transcript_excerpt or "").strip()[:1000]
    return f"""You are judging the VISUAL appeal of a candidate moment for a viral short-form clip (TikTok / Reels / YT Shorts).

The {n_frames} images above were sampled evenly across a {end_sec - start_sec:.1f}-second window (source seconds {start_sec:.1f}–{end_sec:.1f}). The dialogue during this window is:

"{excerpt}"

Rate the VISUAL on 0-100. High scores for:
- Strong facial expressions / visible reactions (shock, joy, anger, surprise, laughter)
- Visually striking action (movement, conflict, stunt, demo, sport, fight, dance)
- Clear, well-framed subjects (face filling the frame, dramatic lighting, etc.)
- Visual payoff that matches a hook (the reveal, the punchline reaction, the result)

Low scores for:
- Static talking head with neutral expression
- Empty stage / no subject in frame / camera on slides only
- Cluttered or poorly framed shots
- Disjointed frames (subject keeps moving off-camera between samples)

Return ONLY a JSON object, no commentary:
{{"visual_score": 0-100, "key_visual": "one short sentence describing what is visually happening", "has_strong_reaction": true|false}}
"""


def _parse_assessment(text: str) -> Optional[VisionAssessment]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0:
        logger.warning(f"[vision] no JSON object in response: {text[:300]}")
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        logger.warning(f"[vision] JSON parse failed: {e}")
        return None
    try:
        return VisionAssessment(
            visual_score=float(obj.get("visual_score", 0)),
            key_visual=str(obj.get("key_visual", ""))[:300],
            has_strong_reaction=bool(obj.get("has_strong_reaction", False)),
        )
    except (TypeError, ValueError) as e:
        logger.warning(f"[vision] malformed assessment: {e}")
        return None


def _encode_image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": data,
        },
    }


def _sample_frames_in_window(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    count: int,
    width_px: int,
) -> List[Path]:
    """Pull `count` evenly-spaced JPEG frames between start_sec and end_sec."""
    if end_sec <= start_sec:
        return []
    tmpdir = Path(tempfile.mkdtemp(prefix="clipfarmer_vision_"))
    out: List[Path] = []
    ffmpeg = settings.ffmpeg_path or "ffmpeg"
    span = end_sec - start_sec
    for i in range(count):
        t = start_sec + span * (i + 1) / (count + 1)
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and out_path.exists():
            out.append(out_path)
    return out
