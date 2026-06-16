"""Speaker diarization — tag each transcript segment with WHO said it.

Whisper gives us text + timestamps but no speaker labels. For podcasts +
interviews this is a big quality gap: the system can't tell when the
host vs guest is speaking, can't attribute quotes correctly in captions,
and can't learn "which speaker's moments perform best".

We use a Claude-based diarizer rather than pyannote.audio because:
- Podcast hosts almost always *introduce* each other ("With me today is X")
  and refer to each other by name throughout, so Claude can attribute
  speakers from CONTEXT very accurately.
- pyannote needs a 1GB+ model + HuggingFace token + audio chunks, adding
  install + GPU complexity.
- Claude can ALSO catch quote-context that pure audio diarization misses
  (e.g. "Sam interrupts X to say...").

Output: same TranscriptSegment list with an added `speaker` field
(string like "Ashlee Vance" or "Speaker A" when names aren't established).

The diarizer is opt-in via `is_podcast=True` on the campaign so we don't
burn API tokens on solo creator content (MrBeast, Boxabl etc) where
diarization doesn't add value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from config import settings


@dataclass
class DiarizedSegment:
    start: float
    end: float
    text: str
    speaker: str  # name if known, else "Speaker A" / "Speaker B" / ...
    confidence: float = 0.0


def diarize_transcript(
    segments: list,
    *,
    title_hint: str = "",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    chunk_size: int = 60,
) -> list[DiarizedSegment]:
    """Process Whisper segments in chunks of `chunk_size`, tagging each
    with a speaker. Returns enriched segment list."""
    api_key = api_key or settings.anthropic_api_key
    model = model or settings.anthropic_model
    if not api_key or not segments:
        return []
    try:
        from engine import llm_compat as anthropic
    except ImportError:
        return []
    client = anthropic.Anthropic(api_key=api_key)

    # The first chunk is largest — Claude has more room to identify speakers
    # by name. Subsequent chunks inherit those names.
    seg_dicts = [_seg_to_dict(s) for s in segments]
    speakers_known: list[str] = []
    out: list[DiarizedSegment] = []

    for i in range(0, len(seg_dicts), chunk_size):
        chunk = seg_dicts[i : i + chunk_size]
        chunk_result = _ask_claude(client, model, chunk, speakers_known, title_hint)
        if not chunk_result:
            # On miss, fill in fallback speaker labels so we don't lose
            # transcript content.
            for s in chunk:
                out.append(DiarizedSegment(
                    start=s["start"], end=s["end"], text=s["text"],
                    speaker="Unknown", confidence=0.0,
                ))
            continue
        for s, tag in zip(chunk, chunk_result):
            spk = (tag.get("speaker") or "Unknown").strip()
            if spk not in ("Unknown", "Speaker") and spk not in speakers_known:
                speakers_known.append(spk)
            out.append(DiarizedSegment(
                start=s["start"], end=s["end"], text=s["text"],
                speaker=spk, confidence=float(tag.get("confidence", 0.5)),
            ))
    logger.info(
        f"[diarize] {len(out)} segments tagged across {len(speakers_known)} "
        f"speakers: {speakers_known[:5]}"
    )
    return out


def _seg_to_dict(s) -> dict:
    if isinstance(s, dict):
        return {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                "text": str(s.get("text", "")).strip()}
    return {
        "start": float(getattr(s, "start", 0)),
        "end": float(getattr(s, "end", 0)),
        "text": str(getattr(s, "text", "")).strip(),
    }


_DIARIZE_PROMPT = """You are tagging speakers on a podcast / interview transcript.

For EACH segment below, return the speaker. Use proper names when they've been introduced ("Ashlee Vance", "Sam Lessin"). If a name hasn't surfaced yet, use placeholder labels ("Speaker A", "Speaker B"). Be CONSISTENT — once you've assigned "Speaker A" to someone, keep using that label until a name is established.

Existing known speakers (use these when applicable): {known}
Title hint: {title}

Return ONLY a JSON array of {{speaker, confidence}} objects, one per input segment, in the same order. Confidence is 0-1.

Segments:
{segments}
"""


def _ask_claude(client, model: str, chunk: list[dict],
                known: list[str], title_hint: str) -> Optional[list[dict]]:
    seg_text = "\n".join(
        f"{i}. [{s['start']:.1f}-{s['end']:.1f}] {s['text'][:200]}"
        for i, s in enumerate(chunk, 1)
    )
    prompt = _DIARIZE_PROMPT.format(
        known=", ".join(known) if known else "(none yet — establish as you go)",
        title=title_hint[:200] or "(none)",
        segments=seg_text[:6000],
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[diarize] Claude call failed: {e}")
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        arr = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"[diarize] unparseable: {text[:200]}")
        return None
    if not isinstance(arr, list):
        return None
    # Pad with Unknowns if Claude returned fewer than expected.
    while len(arr) < len(chunk):
        arr.append({"speaker": "Unknown", "confidence": 0.0})
    return arr[: len(chunk)]


# ----------------------------------------------------------------------
# Convenience: list the speakers + their share of total speaking time.
# ----------------------------------------------------------------------
def speaker_stats(segments: list[DiarizedSegment]) -> list[dict]:
    by_speaker: dict[str, float] = {}
    for s in segments:
        by_speaker[s.speaker] = by_speaker.get(s.speaker, 0.0) + (s.end - s.start)
    total = sum(by_speaker.values()) or 1.0
    out = [
        {"speaker": sp, "seconds": round(secs, 1), "share": round(secs / total, 3)}
        for sp, secs in by_speaker.items()
    ]
    out.sort(key=lambda x: x["seconds"], reverse=True)
    return out


def dominant_speaker_in_range(
    segments: list[DiarizedSegment], start: float, end: float,
) -> Optional[str]:
    """Who's speaking the most between `start` and `end` seconds.
    Used by the captioner / formatter to choose per-speaker styling."""
    by_speaker: dict[str, float] = {}
    for s in segments:
        overlap_start = max(s.start, start)
        overlap_end = min(s.end, end)
        if overlap_end <= overlap_start:
            continue
        by_speaker[s.speaker] = by_speaker.get(s.speaker, 0.0) + (overlap_end - overlap_start)
    if not by_speaker:
        return None
    return max(by_speaker, key=by_speaker.get)
