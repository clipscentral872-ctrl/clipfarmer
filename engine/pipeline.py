"""End-to-end engine: source URL → N captioned 9:16 clips.

Each step is independently retryable so partial progress isn't wasted:
  download   → data/downloads/<id>.mp4
  transcribe → data/downloads/<id>.transcript.json
  score      → in-memory list of ScoredMoment
  cut        → data/clips/<id>__<n>__raw.mp4
  format     → data/clips/<id>__<n>__vert.mp4
  caption    → data/clips/<id>__<n>__final.mp4
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings

from .captioner import Captioner
from .cutter import Cutter
from .downloader import Downloader
from .formatter import Formatter
from .scorer import ClipScorer, ScoredMoment
from .transcriber import Transcriber, TranscriptSegment


@dataclass
class ProducedClip:
    moment: ScoredMoment
    raw_path: Path
    vertical_path: Path
    final_path: Path


class EnginePipeline:
    def __init__(
        self,
        downloader: Optional[Downloader] = None,
        transcriber: Optional[Transcriber] = None,
        scorer: Optional[ClipScorer] = None,
        cutter: Optional[Cutter] = None,
        formatter: Optional[Formatter] = None,
        captioner: Optional[Captioner] = None,
    ) -> None:
        self.downloader = downloader or Downloader()
        self.transcriber = transcriber or Transcriber()
        self.scorer = scorer or ClipScorer()
        self.cutter = cutter or Cutter()
        self.formatter = formatter or Formatter()
        self.captioner = captioner or Captioner()

    # ------------------------------------------------------------------
    def run(
        self,
        source_url: str,
        n_clips: Optional[int] = None,
        campaign_title: Optional[str] = None,
        campaign_rules: Optional[str] = None,
        top_performers: Optional[list[dict]] = None,
        structured_rules: Optional[dict] = None,
        format_mode: str = "smart",
        excluded_ranges: Optional[list[tuple[float, float]]] = None,
        brain_advice: Optional[str] = None,
        diarize: bool = False,
    ) -> List[ProducedClip]:
        n_clips = n_clips or settings.clips_per_source

        # 1. Download.
        video_path = self.downloader.download(source_url)
        source_id = video_path.stem

        # 2. Transcribe (cache).
        transcript_path = video_path.with_suffix(".transcript.json")
        if transcript_path.exists():
            logger.info(f"[engine] using cached transcript: {transcript_path.name}")
            segments = self.transcriber.load_transcript(transcript_path)
        else:
            segments = self.transcriber.transcribe(video_path)
            self.transcriber.save_transcript(segments, transcript_path)

        # 2b. Diarize (opt-in for podcasts) — tag each segment with a speaker name.
        # Cached as a sidecar JSON next to the transcript so we only pay the
        # Claude bill once per source video.
        diarized_segs = None
        if diarize:
            diar_path = video_path.with_suffix(".diarized.json")
            if diar_path.exists():
                try:
                    import json as _json
                    diarized_segs = _json.loads(diar_path.read_text(encoding="utf-8"))
                    logger.info(f"[engine] using cached diarization ({len(diarized_segs)} segs)")
                except Exception:
                    diarized_segs = None
            if diarized_segs is None:
                try:
                    from engine.diarization import diarize_transcript
                    diar = diarize_transcript(segments, title_hint=campaign_title or "")
                    diarized_segs = [
                        {"start": d.start, "end": d.end, "speaker": d.speaker, "text": d.text}
                        for d in diar
                    ]
                    import json as _json
                    diar_path.write_text(_json.dumps(diarized_segs), encoding="utf-8")
                    logger.info(f"[engine] diarized {len(diarized_segs)} segments")
                except Exception as e:
                    logger.warning(f"[engine] diarization failed (continuing without): {e}")
                    diarized_segs = None
        self._diarized = diarized_segs  # consumed by _produce_one

        # 3. Score (transcript + vision blend, optionally guided by top performers and structured rules).
        moments = self.scorer.score(
            segments,
            n_clips=n_clips,
            campaign_title=campaign_title,
            campaign_rules=campaign_rules,
            video_path=video_path,
            top_performers=top_performers,
            structured_rules=structured_rules,
            excluded_ranges=excluded_ranges,
            brain_advice=brain_advice,
        )
        if not moments:
            logger.warning("[engine] scorer returned no moments")
            return []

        # 4–6. Cut / format / caption each moment.
        produced: List[ProducedClip] = []
        for i, m in enumerate(moments, start=1):
            try:
                clip = self._produce_one(video_path, source_id, segments, m, i, format_mode)
                produced.append(clip)
            except Exception as e:
                logger.exception(f"[engine] failed to produce clip {i}: {e}")
        return produced

    # ------------------------------------------------------------------
    def _produce_one(
        self,
        video_path: Path,
        source_id: str,
        segments: List[TranscriptSegment],
        moment: ScoredMoment,
        idx: int,
        format_mode: str,
    ) -> ProducedClip:
        clips_dir = settings.clips_dir
        raw_path = clips_dir / f"{source_id}__{idx:02d}__raw.mp4"
        vert_path = clips_dir / f"{source_id}__{idx:02d}__vert.mp4"
        srt_path = clips_dir / f"{source_id}__{idx:02d}.srt"
        ass_path = clips_dir / f"{source_id}__{idx:02d}.ass"
        final_path = clips_dir / f"{source_id}__{idx:02d}__final.mp4"

        self.cutter.cut(video_path, moment.start_sec, moment.end_sec, raw_path)
        self.formatter.to_vertical(raw_path, vert_path, mode=format_mode)

        # OpusClip-style word-by-word "karaoke" captions when we have word-level
        # timings; otherwise fall back to plain line captions. The hook overlay
        # is burned on top either way.
        ass_built = None
        try:
            ass_built = self.captioner.build_ass(segments, moment.start_sec, ass_path)
        except Exception as e:
            logger.warning(f"[engine] karaoke caption build failed, using line captions: {e}")
        if ass_built is not None:
            self.captioner.burn_ass(vert_path, ass_path, moment.hook_text, final_path)
        else:
            # Pass diarized speakers so podcast clips get "[Ashlee Vance] ..."
            # speaker-prefixed captions when the speaker changes (None = off).
            self.captioner.build_srt(
                segments, moment.start_sec, srt_path,
                diarized=getattr(self, "_diarized", None),
            )
            self.captioner.burn(vert_path, srt_path, moment.hook_text, final_path)
        return ProducedClip(moment=moment, raw_path=raw_path, vertical_path=vert_path, final_path=final_path)
