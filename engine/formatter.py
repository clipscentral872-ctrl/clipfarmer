"""Reformat a clip to 9:16 (1080x1920) — supports several layout modes:
  - "smart" (default): face detection + split-screen for 2 speakers
  - "blur_pad": original 16:9 centered with blurred bg fill
  - "crop": center-crop (zoom on the middle)
  - "letterbox": black bars top/bottom
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings

from .face_crop import build_filter as build_face_filter, decide_layout
from .letterbox import detect_content_box
from .vision_crop import decide_speaker_crop


class FormatError(RuntimeError):
    pass


class Formatter:
    def __init__(self, ffmpeg_path: Optional[str] = None) -> None:
        self.ffmpeg = ffmpeg_path or settings.ffmpeg_path
        if not shutil.which(self.ffmpeg):
            raise FormatError(f"ffmpeg not found at {self.ffmpeg!r}")

    def to_vertical(self, in_path: Path, out_path: Path, mode: str = "smart") -> Path:
        if not in_path.exists():
            raise FormatError(f"input not found: {in_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Mode "auto": choose smart vs group_focus based on subject count.
        if mode == "auto":
            from .auto_framing import decide_format_mode
            mode = decide_format_mode(in_path)
            logger.info(f"[format] auto-selected mode={mode}")

        # Pre-crop: strip baked-in letterbox / pillarbox bars from the source
        # so cinematic content (Jack's Dining Room style) doesn't preserve
        # its built-in black bars through the 9:16 conversion.
        pre_crop_vf = ""
        box = detect_content_box(in_path)
        if box is not None and not box.is_trivial:
            pre_crop_vf = box.ffmpeg_crop + ","

        if mode == "smart":
            vf = None
            # Try vision-guided crop first when enabled — handles 3-person
            # podcast shots and weird compositions the haar cascade can't.
            if settings.enable_vision_crop:
                sc = decide_speaker_crop(
                    in_path,
                    margin_factor=float(settings.vision_crop_margin),
                )
                if sc is not None:
                    logger.info(f"[format] vision crop: {sc.notes}")
                    vf = sc.ffmpeg_vf
                else:
                    logger.info("[format] vision crop unavailable; falling back to haar cascade")
            if vf is None:
                layout = decide_layout(in_path)
                logger.info(f"[format] haar layout chosen: {layout.mode}")
                vf = build_face_filter(layout)
        elif mode == "blur_pad":
            vf = (
                pre_crop_vf +
                "split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=30[bg2];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg2];"
                "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1"
            )
        elif mode == "letterbox":
            vf = (
                pre_crop_vf +
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
        elif mode == "crop":
            vf = pre_crop_vf + "scale=-2:1920,crop=1080:1920,setsar=1"
        elif mode == "group_focus":
            # Tight crop on the group's bounding box + blur-pad to 9:16.
            # Positions subjects in upper-middle so TikTok/IG bottom-rail UI
            # doesn't cover faces. Falls back to blur_pad if vision unavailable.
            vf = self._build_group_focus_vf(in_path, pre_crop_vf)
        else:
            raise FormatError(f"unknown mode: {mode}")

        cmd = [
            self.ffmpeg, "-y",
            "-i", str(in_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "19",
            # Instagram's Graph API rejects non-AAC audio with a generic
            # ProcessingFailedError. YouTube sources often ship Opus inside
            # mp4 containers, so we always transcode audio to AAC here.
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        logger.info(f"[format] {in_path.name} → 9:16 ({mode}) → {out_path.name}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise FormatError(f"ffmpeg format failed: {result.stderr[-1000:]}")
        return out_path

    # ------------------------------------------------------------------
    def _build_group_focus_vf(self, in_path: Path, pre_crop_vf: str) -> str:
        """Vision-detected group bbox → tight crop → 9:16 blur-pad with
        subjects positioned in upper-middle (safe from TikTok/IG bottom UI)."""
        from .auto_framing import decide_group_bbox

        # Source dimensions via ffprobe.
        try:
            r = subprocess.run(
                [settings.ffprobe_path, "-loglevel", "error",
                 "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", str(in_path)],
                capture_output=True, text=True, timeout=15,
            )
            sw, sh = (int(x) for x in r.stdout.strip().split(","))
        except Exception:
            sw, sh = 1920, 1080

        bbox = decide_group_bbox(in_path)
        if bbox is None:
            logger.info("[format][group_focus] no bbox → falling back to plain blur_pad")
            return (
                pre_crop_vf +
                "split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=30[bg2];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg2];"
                "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1"
            )

        # Expand bbox by a safety margin so we don't crop ears/shoulders.
        margin = 0.10
        cx = bbox["x"] - margin
        cy = bbox["y"] - margin
        cw = bbox["w"] + 2 * margin
        ch = bbox["h"] + 2 * margin
        # Clamp 0..1
        cx = max(0.0, min(1.0, cx))
        cy = max(0.0, min(1.0, cy))
        cw = max(0.05, min(1.0 - cx, cw))
        ch = max(0.05, min(1.0 - cy, ch))

        # Convert to absolute pixels.
        px = int(cx * sw)
        py = int(cy * sh)
        pw = int(cw * sw)
        ph = int(ch * sh)
        # Ensure even (ffmpeg requirement).
        if pw % 2:
            pw -= 1
        if ph % 2:
            ph -= 1

        logger.info(
            f"[format][group_focus] bbox crop {pw}x{ph}+{px}+{py} on {sw}x{sh} "
            f"(margin {margin}, subjects upper-middle)"
        )

        # The tight crop becomes the foreground. We scale it to fit 1080
        # wide, then overlay with vertical offset of 300px from top so
        # subjects sit upper-middle (clear of bottom UI).
        # Background is the same crop, scaled-to-cover and blurred.
        # Note: we add a single setsar=1 at the end of the entire chain,
        # not inside split[] blocks (would break the graph).
        return (
            pre_crop_vf +
            f"crop={pw}:{ph}:{px}:{py},"
            "split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=30[bg2];"
            "[fg]scale=1080:-2[fg2];"
            "[bg2][fg2]overlay=(W-w)/2:300,setsar=1"
        )
