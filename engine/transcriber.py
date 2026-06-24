"""Local Whisper transcription.

Returns timestamped segments so the scorer can pick the best 30–60s
windows directly from the transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings


class TranscribeError(RuntimeError):
    pass


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    # Word-level timings [{word, start, end}, ...] in absolute source time.
    # Powers OpusClip-style word-by-word "karaoke" captions. None on older
    # cached transcripts (we fall back to line captions then).
    words: Optional[List[dict]] = None


class Transcriber:
    def __init__(self, model_name: Optional[str] = None, device: Optional[str] = None) -> None:
        self.model_name = model_name or settings.whisper_model
        self.device = device or settings.whisper_device
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import whisper  # openai-whisper
        except ImportError as e:
            raise TranscribeError("openai-whisper not installed") from e
        logger.info(f"[whisper] loading model={self.model_name} device={self.device}")
        self._model = whisper.load_model(self.model_name, device=self.device)
        return self._model

    def transcribe(self, audio_path: Path) -> List[TranscriptSegment]:
        if not audio_path.exists():
            raise TranscribeError(f"audio file not found: {audio_path}")
        model = self._load()
        logger.info(f"[whisper] transcribing {audio_path.name}")
        # fp16 only works on CUDA; force off on CPU to silence the warning.
        fp16 = self.device.startswith("cuda")
        result = model.transcribe(
            str(audio_path),
            verbose=False,
            fp16=fp16,
            condition_on_previous_text=False,
            word_timestamps=True,
        )
        segs = []
        for s in result.get("segments", []):
            if not s.get("text"):
                continue
            words = None
            if s.get("words"):
                words = [
                    {"word": str(w.get("word", "")).strip(), "start": float(w["start"]), "end": float(w["end"])}
                    for w in s["words"]
                    if w.get("word") and w.get("start") is not None and w.get("end") is not None
                ]
                words = [w for w in words if w["word"]] or None
            segs.append(
                TranscriptSegment(
                    start=float(s["start"]), end=float(s["end"]), text=s["text"].strip(), words=words
                )
            )
        logger.info(f"[whisper] {len(segs)} segments, total {segs[-1].end if segs else 0:.1f}s")
        return segs

    def save_transcript(self, segments: List[TranscriptSegment], out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps([asdict(s) for s in segments], indent=2), encoding="utf-8"
        )
        return out_path

    def load_transcript(self, in_path: Path) -> List[TranscriptSegment]:
        data = json.loads(in_path.read_text(encoding="utf-8"))
        return [TranscriptSegment(**d) for d in data]
