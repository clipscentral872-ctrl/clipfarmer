"""Pick the best 30–60s clipping moments using Claude.

Input: a list of TranscriptSegment from Whisper.
Output: up to N ScoredMoment objects, each with start/end/score/reason
and ready-to-post caption + hashtags.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings
from engine.transcriber import TranscriptSegment
from engine.vision_scorer import VisionScorer


class ScoreError(RuntimeError):
    pass


@dataclass
class ScoredMoment:
    start_sec: float
    end_sec: float
    duration_sec: float
    score: float              # 0..100 — final blended score used for ranking
    reason: str
    transcript_excerpt: str
    hook_text: str
    caption_text: str
    hashtags: List[str]
    transcript_score: Optional[float] = None
    visual_score: Optional[float] = None
    visual_notes: Optional[str] = None


class ClipScorer:
    def __init__(
        self,
        anthropic_api_key: Optional[str] = None,
        model: Optional[str] = None,
        vision_scorer: Optional[VisionScorer] = None,
    ) -> None:
        self.api_key = anthropic_api_key or settings.anthropic_api_key
        self.model = model or settings.anthropic_model
        self._client = None
        if not self.api_key:
            raise ScoreError("ANTHROPIC_API_KEY not set in .env")
        self.vision_scorer = vision_scorer  # lazily created in score() if needed

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from engine import llm_compat as anthropic
        except ImportError as e:
            raise ScoreError("anthropic SDK not installed") from e
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def score(
        self,
        segments: List[TranscriptSegment],
        n_clips: Optional[int] = None,
        min_seconds: Optional[int] = None,
        max_seconds: Optional[int] = None,
        campaign_rules: Optional[str] = None,
        campaign_title: Optional[str] = None,
        video_path: Optional[Path] = None,
        top_performers: Optional[list[dict]] = None,
        structured_rules: Optional[dict] = None,
        excluded_ranges: Optional[list[tuple[float, float]]] = None,
        brain_advice: Optional[str] = None,
    ) -> List[ScoredMoment]:
        if not segments:
            return []
        n_clips = n_clips or settings.clips_per_source

        # Structured-rules overrides (from rules_extractor).
        if structured_rules:
            if structured_rules.get("min_seconds"):
                min_seconds = int(structured_rules["min_seconds"])
            if structured_rules.get("max_seconds"):
                max_seconds = int(structured_rules["max_seconds"])

        min_seconds = min_seconds or settings.clip_min_seconds
        max_seconds = max_seconds or settings.clip_max_seconds

        # Ask Claude for more candidates than we need so the vision pass has
        # room to re-rank (visually dull picks drop, striking ones rise).
        use_vision = (
            settings.enable_vision_scoring
            and video_path is not None
            and Path(video_path).exists()
        )
        candidate_target = max(n_clips * 2, n_clips + 2) if use_vision else n_clips

        transcript_text = self._format_transcript(segments)
        prompt = self._build_prompt(
            transcript_text,
            n_clips=candidate_target,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            campaign_title=campaign_title,
            campaign_rules=campaign_rules,
            top_performers=top_performers,
            structured_rules=structured_rules,
            excluded_ranges=excluded_ranges,
            brain_advice=brain_advice,
        )

        client = self._get_client()
        logger.info(f"[claude] scoring {len(segments)} segments via {self.model} (asking for {candidate_target} candidates)")
        resp = client.messages.create(
            model=self.model,
            max_tokens=4_000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        moments = self._parse_response(text, segments)
        for m in moments:
            m.transcript_score = m.score
        moments = self._postprocess(moments, segments, min_seconds, max_seconds)

        # Drop anything that overlaps a previously-used / previously-rejected
        # time range — Chris should never see the same moment twice.
        if excluded_ranges:
            before = len(moments)
            moments = [
                m for m in moments
                if not any(_overlap_ratio(m.start_sec, m.end_sec, a, b) > 0.4
                           for (a, b) in excluded_ranges)
            ]
            if len(moments) < before:
                logger.info(f"[claude] dropped {before - len(moments)} moment(s) overlapping prior clips")

        # Drop moments whose transcript contains a forbidden phrase.
        if structured_rules:
            moments = self._filter_forbidden(moments, structured_rules)
            moments = self._apply_required_caption(moments, structured_rules)

        if use_vision and moments:
            moments = self._apply_vision_scoring(moments, Path(video_path))

        moments.sort(key=lambda m: m.score, reverse=True)
        logger.info(f"[claude] returned {len(moments)} viable moment(s)")
        return moments[:n_clips]

    # ------------------------------------------------------------------
    def _filter_forbidden(self, moments: List[ScoredMoment], rules: dict) -> List[ScoredMoment]:
        forbidden = [p.strip() for p in rules.get("forbidden_phrases") or [] if p and p.strip()]
        if not forbidden:
            return moments
        kept = []
        for m in moments:
            hit = _find_forbidden(m.transcript_excerpt or "", forbidden)
            if hit:
                logger.warning(f"[rules] dropping moment {m.start_sec:.1f}-{m.end_sec:.1f} — forbidden phrase {hit!r}")
                continue
            kept.append(m)
        return kept

    def _apply_required_caption(self, moments: List[ScoredMoment], rules: dict) -> List[ScoredMoment]:
        required = rules.get("required_caption")
        handling = (rules.get("caption_handling") or "").lower()
        if not required:
            return moments
        for m in moments:
            ai_caption = (m.caption_text or "").strip()
            if handling == "starts_with":
                m.caption_text = required.strip() + ("\n\n" + ai_caption if ai_caption else "")
            elif handling in ("exact", "", "none"):
                # "exact" means use the required caption verbatim; preserve the
                # AI's text in hook_text so we still have something punchy for
                # the on-screen overlay.
                m.caption_text = required.strip()
            elif handling == "contains":
                if required.lower() not in ai_caption.lower():
                    m.caption_text = (ai_caption + "\n\n" + required.strip()).strip()
        return moments

    # ------------------------------------------------------------------
    def _apply_vision_scoring(
        self,
        moments: List[ScoredMoment],
        video_path: Path,
    ) -> List[ScoredMoment]:
        if self.vision_scorer is None:
            self.vision_scorer = VisionScorer(
                anthropic_api_key=self.api_key,
                model=self.model,
            )
        logger.info(f"[vision] assessing {len(moments)} candidate moment(s)")
        w_text = float(settings.vision_text_weight)
        w_vis = float(settings.vision_visual_weight)
        for m in moments:
            assessment = self.vision_scorer.assess(
                video_path=video_path,
                start_sec=m.start_sec,
                end_sec=m.end_sec,
                transcript_excerpt=m.transcript_excerpt,
            )
            if assessment is None:
                continue
            m.visual_score = assessment.visual_score
            m.visual_notes = assessment.key_visual
            blended = (w_text * (m.transcript_score or m.score) + w_vis * assessment.visual_score) / (w_text + w_vis)
            logger.info(
                f"[vision] {m.start_sec:.1f}-{m.end_sec:.1f}: "
                f"txt={m.transcript_score:.0f} vis={assessment.visual_score:.0f} "
                f"→ blended={blended:.0f} ({assessment.key_visual[:80]})"
            )
            m.score = round(blended, 2)
        return moments

    # ------------------------------------------------------------------
    def _format_transcript(self, segments: List[TranscriptSegment]) -> str:
        lines = []
        for s in segments:
            lines.append(f"[{s.start:.1f}-{s.end:.1f}] {s.text}")
        return "\n".join(lines)

    def _build_prompt(
        self,
        transcript: str,
        n_clips: int,
        min_seconds: int,
        max_seconds: int,
        campaign_title: Optional[str],
        campaign_rules: Optional[str],
        top_performers: Optional[list[dict]] = None,
        structured_rules: Optional[dict] = None,
        excluded_ranges: Optional[list[tuple[float, float]]] = None,
        brain_advice: Optional[str] = None,
    ) -> str:
        ctx = ""
        if campaign_title:
            ctx += f"Campaign: {campaign_title}\n"
        if structured_rules:
            ctx += _format_structured_rules(structured_rules) + "\n"
        elif campaign_rules:
            ctx += f"Campaign rules (must follow):\n{campaign_rules.strip()[:2000]}\n"
        if top_performers:
            ctx += "\n" + _format_top_performers(top_performers) + "\n"
        if brain_advice:
            ctx += "\n" + brain_advice.strip() + "\n"
        if excluded_ranges:
            ranges_str = ", ".join(f"{a:.1f}-{b:.1f}s" for (a, b) in excluded_ranges[:20])
            ctx += (
                "\nThese time ranges have already been clipped (and either posted "
                "or rejected by the user). Do NOT pick a moment that overlaps any "
                f"of them: {ranges_str}\n"
            )

        return f"""You are a viral short-form video editor. From the timestamped transcript below, pick the {n_clips} most clip-worthy moments for TikTok / YouTube Shorts / Instagram Reels.

{ctx}
Each pick must:
- Be between {min_seconds} and {max_seconds} seconds long.
- Stand alone (a viewer with no prior context understands the moment).
- Have a strong hook in the first 3 seconds (intrigue, conflict, claim, payoff promise).
- Follow any campaign rules above (required hashtags, mentions, brand language).
- If "Top performing clips in this campaign" are listed above, lean toward moments that match their angle / energy / structure — those formats are already proven to earn here.

For each moment, give:
- start_sec, end_sec (decimals OK; must fit inside the transcript range)
- score (0–100: how viral-likely)
- reason (one sentence: why this moment will work)
- transcript_excerpt (the actual transcribed text inside the window)
- hook_text (≤ 60 chars; the on-screen overlay for the first 3 seconds)
- caption_text (the post caption — punchy, ≤ 200 chars; include any required hashtags inline)
- hashtags (array of 3–6 hashtags WITHOUT the # symbol)

Return ONLY a JSON array, no commentary. Example:
[{{"start_sec": 12.4, "end_sec": 58.2, "score": 87, "reason": "...", "transcript_excerpt": "...", "hook_text": "...", "caption_text": "...", "hashtags": ["...", "..."]}}]

Transcript:
{transcript}
"""

    def _parse_response(self, text: str, segments: List[TranscriptSegment]) -> List[ScoredMoment]:
        # Strip code fences if Claude added them.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        # Find the first JSON array.
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start < 0 or end < 0:
            raise ScoreError(f"Claude returned no JSON array. First 500 chars: {text[:500]}")
        payload = cleaned[start : end + 1]
        try:
            items = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ScoreError(f"could not parse Claude JSON: {e}\n{payload[:1000]}")
        out: List[ScoredMoment] = []
        for it in items:
            try:
                start_sec = float(it["start_sec"])
                end_sec = float(it["end_sec"])
                out.append(ScoredMoment(
                    start_sec=start_sec,
                    end_sec=end_sec,
                    duration_sec=round(end_sec - start_sec, 2),
                    score=float(it.get("score", 0)),
                    reason=str(it.get("reason", ""))[:500],
                    transcript_excerpt=str(it.get("transcript_excerpt", ""))[:2000],
                    hook_text=str(it.get("hook_text", ""))[:80],
                    caption_text=str(it.get("caption_text", ""))[:500],
                    hashtags=[str(h).lstrip("#") for h in it.get("hashtags", [])][:10],
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"[claude] skipping malformed item: {e}")
        return out

    def _postprocess(
        self,
        moments: List[ScoredMoment],
        segments: List[TranscriptSegment],
        min_seconds: int,
        max_seconds: int,
    ) -> List[ScoredMoment]:
        if not segments:
            return []
        upper = segments[-1].end
        cleaned: List[ScoredMoment] = []
        for m in moments:
            s = max(0.0, m.start_sec)
            e = min(upper, m.end_sec)
            dur = e - s
            if dur < min_seconds:
                continue
            if dur > max_seconds:
                e = s + max_seconds
                dur = max_seconds
            # Drop if it overlaps an earlier higher-scored moment by > 50%.
            overlap = any(_overlap_ratio(s, e, c.start_sec, c.end_sec) > 0.5 for c in cleaned)
            if overlap:
                continue
            m.start_sec = round(s, 2)
            m.end_sec = round(e, 2)
            m.duration_sec = round(dur, 2)
            cleaned.append(m)
        return cleaned


def _find_forbidden(haystack: str, phrases: list[str]) -> Optional[str]:
    """Return the first phrase whose whole-word (case-insensitive) appears in haystack.

    Uses regex word boundaries so 'ai' does NOT match 'again' / 'main' / 'said'.
    Multi-word phrases just become escaped patterns surrounded by \\b.
    """
    hay = haystack or ""
    for p in phrases:
        p = (p or "").strip()
        if not p:
            continue
        try:
            if re.search(r"\b" + re.escape(p) + r"\b", hay, re.IGNORECASE):
                return p
        except re.error:
            if p.lower() in hay.lower():
                return p
    return None


def _overlap_ratio(a1: float, a2: float, b1: float, b2: float) -> float:
    inter = max(0.0, min(a2, b2) - max(a1, b1))
    span = min(a2 - a1, b2 - b1)
    return inter / span if span > 0 else 0.0


def _format_structured_rules(rules: dict) -> str:
    """Render structured_rules JSON as a tight section for the scorer prompt."""
    parts = ["Campaign rules (HARD constraints — every pick must comply):"]
    if rules.get("summary"):
        parts.append(f"- Goal: {rules['summary']}")
    if rules.get("treatment_notes"):
        parts.append(f"- Style/treatment: {rules['treatment_notes']}")
    if rules.get("do_list"):
        parts.append("- DO: " + "; ".join(rules["do_list"][:8]))
    if rules.get("dont_list"):
        parts.append("- DON'T: " + "; ".join(rules["dont_list"][:8]))
    if rules.get("forbidden_phrases"):
        parts.append(
            "- The clip's spoken content MUST NOT contain any of these phrases: "
            + ", ".join(f"'{p}'" for p in rules["forbidden_phrases"][:12])
        )
    if rules.get("required_hashtags"):
        parts.append("- Required hashtags in every caption: " + " ".join(rules["required_hashtags"]))
    if rules.get("required_mentions"):
        parts.append("- Required mentions: " + " ".join(rules["required_mentions"]))
    if rules.get("required_caption"):
        parts.append(
            "- A FIXED caption is required for this campaign and will be applied "
            "automatically after you respond — your `caption_text` will be used "
            "only as fallback / supplemental description. Focus on hook_text and "
            "moment selection."
        )
    return "\n".join(parts)


def _format_top_performers(performers: list[dict]) -> str:
    """Render the top-performer JSON list as compact bullets for the prompt.

    Accepts loose dicts — anything that may have a subset of:
    title, views, est_earnings, platform, url, hook, length_sec, notes.
    """
    lines = ["Top performing clips in this campaign (style signal — match this energy):"]
    for p in performers[:8]:
        bits = []
        title = (p.get("title") or p.get("hook") or "").strip()
        if title:
            bits.append(f'"{title[:120]}"')
        if p.get("views"):
            bits.append(f"{p['views']} views")
        if p.get("est_earnings"):
            bits.append(f"~${p['est_earnings']} earned")
        if p.get("platform"):
            bits.append(p["platform"])
        if p.get("length_sec"):
            bits.append(f"{p['length_sec']}s")
        if p.get("notes"):
            bits.append(p["notes"][:200])
        if bits:
            lines.append(f"- {' | '.join(bits)}")
    return "\n".join(lines) if len(lines) > 1 else ""
