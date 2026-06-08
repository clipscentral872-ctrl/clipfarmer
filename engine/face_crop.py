"""Smart 9:16 crop based on where faces actually are in the frame.

Strategy:
  1. Sample a handful of frames from across the clip with ffmpeg.
  2. Run an OpenCV face detector on each sample.
  3. Aggregate face positions.
  4. Decide a layout:
     - 0 faces  → blur_pad (safe fallback)
     - 1 face   → crop a 9:16 window centered on that face
     - 2 faces  → split-screen, top half = one face, bottom = the other
     - 3+ faces → blur_pad (too busy to crop cleanly)

The actual ffmpeg render still happens in formatter.py; this module
only decides WHICH mode + WHERE to crop, then returns the filter string.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from config import settings


@dataclass
class FaceLayout:
    mode: str                       # "crop_face" | "split_two" | "blur_pad"
    center_x: Optional[float] = None  # 0..1 (used for crop_face)
    top_face_y: Optional[float] = None  # 0..1 (used for split_two, top tile center)
    bottom_face_y: Optional[float] = None  # 0..1 (used for split_two, bottom tile center)


def decide_layout(video_path: Path, sample_count: int = 5) -> FaceLayout:
    """Inspect a video and decide the best 9:16 layout for its faces."""
    if not video_path.exists():
        return FaceLayout(mode="blur_pad")

    try:
        import cv2  # noqa: F401
    except ImportError:
        logger.warning("[face_crop] opencv not installed — falling back to blur_pad")
        return FaceLayout(mode="blur_pad")

    frames = _sample_frames(video_path, sample_count)
    if not frames:
        return FaceLayout(mode="blur_pad")

    face_lists = [_detect_faces(f) for f in frames]
    # Clean up sampled frames now that detection is done.
    for f in frames:
        try:
            f.unlink()
        except Exception:
            pass

    counts = [len(fl) for fl in face_lists]
    if not counts:
        return FaceLayout(mode="blur_pad")

    # Pick the most common face-count across samples.
    typical = max(set(counts), key=counts.count)
    if typical == 0:
        return FaceLayout(mode="blur_pad")
    if typical >= 3:
        return FaceLayout(mode="blur_pad")

    # 1 face: average center across frames that had exactly 1 face.
    if typical == 1:
        centers = []
        for fl, frame in zip(face_lists, frames):
            if len(fl) != 1:
                continue
            x, y, w, h, frame_w, frame_h = fl[0]
            centers.append(((x + w / 2) / frame_w, (y + h / 2) / frame_h))
        if not centers:
            return FaceLayout(mode="blur_pad")
        cx = sum(c[0] for c in centers) / len(centers)
        return FaceLayout(mode="crop_face", center_x=cx)

    # 2 faces: pick the frame with exactly 2 faces and use those as the
    # top / bottom tiles (sort by y).
    for fl in face_lists:
        if len(fl) == 2:
            faces_sorted = sorted(fl, key=lambda f: f[1])  # by y
            (x1, y1, w1, h1, fw, fh) = faces_sorted[0]
            (x2, y2, w2, h2, _, _) = faces_sorted[1]
            return FaceLayout(
                mode="split_two",
                top_face_y=(y1 + h1 / 2) / fh,
                bottom_face_y=(y2 + h2 / 2) / fh,
            )
    return FaceLayout(mode="blur_pad")


# ----------------------------------------------------------------------
def _sample_frames(video_path: Path, count: int) -> List[Path]:
    """Pull `count` evenly-spaced JPEG frames from the video."""
    # Get duration with ffprobe.
    ffprobe = settings.ffprobe_path or "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip())
    except Exception as e:
        logger.warning(f"[face_crop] ffprobe failed: {e}")
        return []

    if duration <= 0:
        return []

    tmpdir = Path(tempfile.mkdtemp(prefix="clipfarmer_frames_"))
    out: List[Path] = []
    ffmpeg = settings.ffmpeg_path or "ffmpeg"
    # Evenly spaced sample points avoiding the very start and end.
    for i in range(count):
        t = duration * (i + 1) / (count + 1)
        out_path = tmpdir / f"frame_{i:02d}.jpg"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{t:.2f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(out_path),
        ]
        r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r2.returncode == 0 and out_path.exists():
            out.append(out_path)
    return out


def _detect_faces(image_path: Path) -> List[Tuple[int, int, int, int, int, int]]:
    """Return list of (x, y, w, h, frame_w, frame_h) face boxes."""
    import cv2

    img = cv2.imread(str(image_path))
    if img is None:
        return []
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.15,
        minNeighbors=5,
        minSize=(80, 80),
    )
    out = []
    for (x, y, fw, fh) in faces:
        out.append((int(x), int(y), int(fw), int(fh), w, h))
    return out


# ----------------------------------------------------------------------
def build_filter(layout: FaceLayout) -> str:
    """Convert a FaceLayout into an ffmpeg -vf filter string for a 1080x1920 output."""
    if layout.mode == "crop_face" and layout.center_x is not None:
        # The source is 1920x1080 (assumed 16:9). We crop a 1080x1920 (no —
        # we crop a vertical-aspect window). Trick: scale source up so its
        # height becomes 1920, then crop 1080 wide centered on the face.
        # The face x-center in the SOURCE maps to x-center in the SCALED.
        # We'll express the crop x with `in_w` so ffmpeg handles whatever
        # source resolution we get.
        cx = max(0.0, min(1.0, layout.center_x))
        # crop x = (in_w * cx) - 540 (half of 1080), clamped to [0, in_w-1080]
        return (
            f"scale=-2:1920,"
            f"crop=1080:1920:"
            f"x='clip(iw*{cx:.3f}-540,0,iw-1080)':y=0,setsar=1"
        )

    if layout.mode == "split_two" and layout.top_face_y is not None and layout.bottom_face_y is not None:
        # Two stacked tiles of 1080x960 each. Each tile is the same source
        # scaled to 1080 wide, then cropped vertically around the relevant
        # face's y position.
        ty = max(0.0, min(1.0, layout.top_face_y))
        by = max(0.0, min(1.0, layout.bottom_face_y))
        # Each tile's source-relative y center, expressed against the
        # source's scaled height (after scale=1080:-2). The actual crop
        # math is done by ffmpeg using in_h on the scaled stream.
        return (
            f"split=2[t][b];"
            f"[t]scale=1080:-2,crop=1080:960:0:'clip(ih*{ty:.3f}-480,0,ih-960)'[tt];"
            f"[b]scale=1080:-2,crop=1080:960:0:'clip(ih*{by:.3f}-480,0,ih-960)'[bb];"
            f"[tt][bb]vstack=inputs=2,setsar=1"
        )

    # Fallback to the safe blur_pad layout.
    return (
        "split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=30[bg2];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg2];"
        "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
