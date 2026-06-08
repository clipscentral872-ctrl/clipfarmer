"""Burn auto-captions (SRT) + a 3-second hook text overlay onto a clip."""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings
from engine.transcriber import TranscriptSegment


class CaptionError(RuntimeError):
    pass


class Captioner:
    def __init__(self, ffmpeg_path: Optional[str] = None) -> None:
        self.ffmpeg = ffmpeg_path or settings.ffmpeg_path
        if not shutil.which(self.ffmpeg):
            raise CaptionError(f"ffmpeg not found at {self.ffmpeg!r}")

    # ------------------------------------------------------------------
    def build_srt(
        self,
        segments: List[TranscriptSegment],
        offset_sec: float,
        out_path: Path,
        diarized: Optional[list] = None,
    ) -> Path:
        """Build an SRT covering the clip window. `offset_sec` is the parent
        clip's start_sec; we shift each segment's timestamps so they start at 0.

        If `diarized` is provided (list of DiarizedSegment), prefix each
        caption with the speaker name when it changes — useful for podcast
        clips with 2+ speakers so viewers know who's talking."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        idx = 1
        prev_speaker = None
        for s in segments:
            if s.end <= offset_sec:
                continue
            start = max(0.0, s.start - offset_sec)
            end = max(0.0, s.end - offset_sec)
            if end <= start:
                continue
            text = s.text.strip()
            # Look up the speaker label by timestamp overlap.
            if diarized:
                speaker = _speaker_at(diarized, s.start, s.end)
                if speaker and speaker != prev_speaker and speaker not in ("Unknown",):
                    text = f"[{speaker}] {text}"
                    prev_speaker = speaker
            lines.append(str(idx))
            lines.append(f"{_fmt_srt(start)} --> {_fmt_srt(end)}")
            lines.append(text)
            lines.append("")
            idx += 1
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path

    # ------------------------------------------------------------------
    def burn(
        self,
        in_path: Path,
        srt_path: Path,
        hook_text: str,
        out_path: Path,
        hook_duration: float = 3.0,
    ) -> Path:
        if not in_path.exists():
            raise CaptionError(f"input not found: {in_path}")
        if not srt_path.exists():
            raise CaptionError(f"srt not found: {srt_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Two-pass burn — much more robust than a single chained -vf because
        # the subtitles filter (with Windows paths) and drawtext (with
        # expressions containing commas) interact badly when combined.
        tmp_with_subs = out_path.with_name(out_path.stem + "__subs.mp4")
        try:
            self._burn_subs(in_path, srt_path, tmp_with_subs)
            self._burn_hook(tmp_with_subs, hook_text, hook_duration, out_path)
        finally:
            if tmp_with_subs.exists():
                try:
                    tmp_with_subs.unlink()
                except Exception:
                    pass
        return out_path

    def _burn_subs(self, in_path: Path, srt_path: Path, out_path: Path) -> None:
        srt_arg = str(srt_path).replace("\\", "/").replace(":", "\\:")
        style = (
            "FontName=Arial,FontSize=22,"
            "PrimaryColour=&HFFFFFFFF&,OutlineColour=&HFF000000&,"
            "Outline=2,Shadow=0,Alignment=2,MarginV=80"
        )
        vf = f"subtitles='{srt_arg}':force_style='{style}'"
        cmd = [
            self.ffmpeg, "-y",
            "-i", str(in_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        logger.info(f"[caption] subs pass: {in_path.name} → {out_path.name}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise CaptionError(f"subs burn failed:\n{r.stderr[-3000:]}")

    def _burn_hook(self, in_path: Path, hook_text: str, hook_duration: float, out_path: Path) -> None:
        # Hook overlay only for the first N seconds. We render the hook
        # text as a transparent PNG with Pillow and then composite it via
        # ffmpeg's overlay filter. That avoids drawtext's Windows-path /
        # comma-escaping nightmares entirely.
        if not hook_text:
            cmd = [
                self.ffmpeg, "-y", "-i", str(in_path),
                "-c", "copy", "-movflags", "+faststart",
                str(out_path),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise CaptionError(f"hook copy failed:\n{r.stderr[-3000:]}")
            return

        png_path = out_path.with_name(out_path.stem + "__hook.png")
        try:
            _render_hook_png(hook_text, png_path, video_width=1080)
            vf = (
                f"[0:v][1:v]overlay=(W-w)/2:H*0.18:"
                f"enable='lte(t,{hook_duration:.2f})'[v]"
            )
            cmd = [
                self.ffmpeg, "-y",
                "-i", str(in_path),
                "-i", str(png_path),
                "-filter_complex", vf,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ]
            logger.info(f"[caption] hook pass: {in_path.name} → {out_path.name}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise CaptionError(f"hook burn failed:\n{r.stderr[-3000:]}")
        finally:
            if png_path.exists():
                try:
                    png_path.unlink()
                except Exception:
                    pass


def _render_hook_png(text: str, out_path: Path, video_width: int = 1080) -> Path:
    """Render hook text as a transparent PNG, sized to fit the video width."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_default_font()
    # Big bold text. Drop size if string is long so it wraps reasonably.
    base_size = 72
    max_text_width = int(video_width * 0.86)

    def make_font(size: int):
        return ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()

    font = make_font(base_size)
    lines = _wrap_text_pillow(text, font, max_text_width)
    while len(lines) > 3 and base_size > 36:
        base_size -= 8
        font = make_font(base_size)
        lines = _wrap_text_pillow(text, font, max_text_width)

    # Measure final layout.
    dummy = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(dummy)
    line_heights = [d.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    line_widths = [d.textbbox((0, 0), ln, font=font)[2] for ln in lines]
    pad_x, pad_y, gap = 36, 24, 10
    content_w = max(line_widths) if line_widths else 0
    content_h = sum(line_heights) + gap * (len(lines) - 1) if line_heights else 0
    W = min(video_width - 80, content_w + pad_x * 2)
    H = content_h + pad_y * 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded background pill.
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=24, fill=(0, 0, 0, 180))
    y = pad_y
    for ln, lh in zip(lines, line_heights):
        lw = d.textbbox((0, 0), ln, font=font)[2]
        x = (W - lw) // 2
        d.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh + gap
    img.save(out_path, "PNG")
    return out_path


def _wrap_text_pillow(text: str, font, max_width_px: int) -> list[str]:
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = f"{current} {w}"
        if d.textbbox((0, 0), candidate, font=font)[2] <= max_width_px:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def _find_default_font() -> Optional[str]:
    """Find a sensible default font file. Windows-friendly defaults first."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c.replace("\\", "/")
    return None


def _speaker_at(diarized: list, start: float, end: float) -> Optional[str]:
    """Return the dominant speaker label whose timespan overlaps [start, end]."""
    best_speaker = None
    best_overlap = 0.0
    for d in diarized:
        d_start = getattr(d, "start", None) if not isinstance(d, dict) else d.get("start")
        d_end = getattr(d, "end", None) if not isinstance(d, dict) else d.get("end")
        sp = getattr(d, "speaker", None) if not isinstance(d, dict) else d.get("speaker")
        if d_start is None or d_end is None or not sp:
            continue
        overlap = max(0.0, min(end, d_end) - max(start, d_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = sp
    return best_speaker


def _fmt_srt(t: float) -> str:
    td = timedelta(seconds=t)
    hours, remainder = divmod(td.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{ms:03d}"


def _ff_escape(s: str) -> str:
    # ffmpeg drawtext: escape :, ', \, ,
    return (
        s.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )
