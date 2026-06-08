"""Auto-find a source video for a campaign that allows open sourcing.

For campaigns whose brief doesn't restrict where content can come from
(empty `structured_rules.source_must_match`), we can find a fresh source
ourselves: search YouTube, rank by relevance to the campaign, and
download the top result.

Flow:
  1. Claude builds a YouTube search query from the campaign's brief +
     required mentions/hashtags + treatment notes.
  2. yt-dlp does a flat `ytsearchN:` lookup — returns N candidate
     videos with metadata (title, duration, channel, views, upload date)
     without downloading.
  3. Claude ranks the candidates against the campaign brief and picks
     the best one (filters out shorts, music videos, unrelated, etc.).
  4. yt-dlp downloads the picked URL into `data/downloads/`.
  5. The path is saved as the campaign's `current_source_path` so the
     scheduler can use it immediately.

This module is OPT-IN per campaign: scheduler calls it only when the
campaign has no `current_source_path` AND no `source_must_match`
restriction. Source-restricted campaigns (Bloom, Substack) are never
auto-sourced.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings


@dataclass
class SourceCandidate:
    url: str
    title: str = ""
    duration_sec: float = 0.0
    channel: str = ""
    view_count: int = 0
    upload_date: str = ""

    def short_blurb(self) -> str:
        mins = self.duration_sec / 60.0 if self.duration_sec else 0.0
        return (
            f'"{self.title[:120]}" | {self.channel} | '
            f"{mins:.1f}min | {self.view_count:,} views | {self.upload_date}"
        )


class SourceFinderError(RuntimeError):
    pass


# ----------------------------------------------------------------------
def find_source(
    campaign: dict,
    *,
    n_candidates: int = 10,
    min_duration_sec: int = 180,
) -> Optional[SourceCandidate]:
    """Search YouTube, rank, return the best candidate. Does NOT download."""
    structured = _parse_structured_rules(campaign)
    if (structured.get("source_must_match") or []):
        logger.warning(
            f"[finder] campaign #{campaign.get('id')} has source_must_match — "
            f"won't auto-find. Use the campaign's approved source."
        )
        return None

    query = _build_search_query(campaign, structured)
    if not query:
        logger.warning("[finder] could not build a search query")
        return None
    logger.info(f"[finder] query: {query!r}")

    candidates = _search_youtube(query, n_candidates)
    if not candidates:
        logger.warning("[finder] yt-dlp returned 0 candidates")
        return None
    # Drop shorts and very short videos — we need longform to clip from.
    candidates = [c for c in candidates if c.duration_sec >= min_duration_sec]
    if not candidates:
        logger.warning(f"[finder] all candidates were under {min_duration_sec}s")
        return None
    logger.info(f"[finder] {len(candidates)} candidate(s) after filtering")

    pick = _rank_candidates(campaign, structured, candidates)
    if pick is None:
        logger.warning("[finder] ranker did not pick a winner; falling back to most-viewed")
        pick = max(candidates, key=lambda c: c.view_count)
    logger.info(f"[finder] picked: {pick.url}  — {pick.short_blurb()}")
    return pick


def find_and_download_source(campaign: dict) -> Optional[Path]:
    """Find a source AND download it. Returns the local path or None."""
    pick = find_source(campaign)
    if not pick:
        return None
    from engine.downloader import Downloader
    try:
        return Downloader().download(pick.url)
    except Exception as e:
        logger.warning(f"[finder] download failed for {pick.url}: {e}")
        return None


# ----------------------------------------------------------------------
def _parse_structured_rules(campaign: dict) -> dict:
    raw = campaign.get("structured_rules")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


# ----------------------------------------------------------------------
def _build_search_query(campaign: dict, structured: dict) -> str:
    """Use Claude to turn the campaign brief into a tight YouTube query."""
    api_key = settings.anthropic_api_key
    if not api_key:
        logger.warning("[finder] no Anthropic key; using naive query")
        return _naive_query(campaign, structured)

    summary = (structured.get("summary") or "").strip()
    notes = (structured.get("treatment_notes") or "").strip()
    mentions = structured.get("required_mentions") or []
    hashtags = structured.get("required_hashtags") or []
    title = campaign.get("title", "")

    prompt = f"""You are constructing a YouTube search query that will find LONG-FORM source video footage to be clipped into short-form vertical (TikTok / Reels / Shorts) for a clip-farming campaign.

Campaign title: {title}
Summary: {summary}
Treatment notes: {notes}
Required mentions: {mentions}
Required hashtags: {hashtags}

Return ONLY the search query as plain text on a single line — no quotes, no explanation. The query should:
- Find long-form (not shorts) content the campaign is about.
- Lean toward the creator / brand / event named in the campaign — e.g. their channel name without the @.
- Avoid year filters unless the campaign explicitly targets a recent event.
- Be 3-8 words, the kind of thing you'd actually type into YouTube's search bar.

Search query:"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # Strip any quotes / "Search query:" prefix Claude added defensively.
        text = re.sub(r'^["\']|["\']$', "", text)
        text = re.sub(r"(?i)^search\s+query\s*:\s*", "", text).strip()
        return text.splitlines()[0][:200] or _naive_query(campaign, structured)
    except Exception as e:
        logger.warning(f"[finder] Claude query build failed, using naive: {e}")
        return _naive_query(campaign, structured)


def _naive_query(campaign: dict, structured: dict) -> str:
    """Fallback query when Claude isn't available."""
    title = campaign.get("title", "")
    # Strip our internal annotations like "[Podcast Clipping]" / "[Viral Clipping]"
    base = re.sub(r"\[[^\]]+\]", "", title).strip()
    mentions = structured.get("required_mentions") or []
    if mentions:
        return f"{base} {mentions[0].lstrip('@')}"
    return base


# ----------------------------------------------------------------------
def _youtube_search_cookie_opts() -> dict:
    """Pass YouTube cookies to yt-dlp search/list calls when present so
    GitHub Actions runners don't trip the bot challenge.  See
    `engine.downloader._youtube_cookie_opts` for the matching download-side
    helper and the .auth/youtube-cookies.txt setup."""
    cookies_path = settings.project_root / ".auth" / "youtube-cookies.txt"
    if cookies_path.exists():
        return {"cookiefile": str(cookies_path)}
    return {}


def _search_youtube(query: str, n: int) -> List[SourceCandidate]:
    """Run yt-dlp ytsearch and parse the JSON metadata."""
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise SourceFinderError("yt-dlp not installed")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,  # don't fetch each video, just list
        "skip_download": True,
        "noplaylist": True,
        **_youtube_search_cookie_opts(),
    }
    out: List[SourceCandidate] = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    except Exception as e:
        logger.warning(f"[finder] yt-dlp search failed: {e}")
        return out
    if not info:
        return out
    entries = info.get("entries") or []
    for e in entries:
        if not e or not e.get("url"):
            continue
        url = e.get("url")
        # yt-dlp's flat-playlist sometimes returns plain video ids — normalise.
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        out.append(SourceCandidate(
            url=url,
            title=e.get("title") or "",
            duration_sec=float(e.get("duration") or 0),
            channel=e.get("channel") or e.get("uploader") or "",
            view_count=int(e.get("view_count") or 0),
            upload_date=str(e.get("upload_date") or ""),
        ))
    return out


# ----------------------------------------------------------------------
def _rank_candidates(
    campaign: dict, structured: dict, candidates: List[SourceCandidate],
) -> Optional[SourceCandidate]:
    api_key = settings.anthropic_api_key
    if not api_key or not candidates:
        return None

    blurbs = []
    for i, c in enumerate(candidates):
        blurbs.append(f"[{i}] {c.short_blurb()}")
    blurbs_block = "\n".join(blurbs)

    summary = (structured.get("summary") or "").strip()
    notes = (structured.get("treatment_notes") or "").strip()
    title = campaign.get("title", "")

    prompt = f"""You are picking the best YouTube video to use as source footage for a clipping campaign. We will Whisper-transcribe the video, then have Claude pick 30-60s viral moments to repost on TikTok / Reels / Shorts.

Campaign title: {title}
Summary: {summary}
Treatment notes: {notes}

Candidates (indexed):
{blurbs_block}

Pick the SINGLE best candidate. Prefer:
- The official creator / brand channel for the campaign
- Long-form content (so we have plenty of moments to pick from)
- Recent uploads when the campaign is about a recent event
- High view count as a quality / popularity signal (but not the only signal)

AVOID:
- Music videos / pure musical performances (Whisper won't transcribe singing well)
- Compilations of other people's clips (sources should be original)
- Anything that obviously doesn't match the campaign

Return ONLY a JSON object: {{"index": N, "reason": "one short sentence"}}. If none are usable, return {{"index": -1, "reason": "..."}}."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.warning(f"[finder] ranker call failed: {e}")
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    s = cleaned.find("{")
    e_ = cleaned.rfind("}")
    if s < 0 or e_ < 0:
        return None
    try:
        obj = json.loads(cleaned[s : e_ + 1])
        idx = int(obj.get("index", -1))
        reason = str(obj.get("reason", ""))[:200]
    except Exception:
        return None
    if idx < 0 or idx >= len(candidates):
        return None
    logger.info(f"[finder] ranker reason: {reason}")
    return candidates[idx]
