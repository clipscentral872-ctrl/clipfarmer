"""Deep competitor analysis — downloads winning clips and extracts the
specific elements that make them work.

Where `competitor_learner.py` does metadata-only (title + description),
this module does the heavy version:

  1. Download each top-performer clip via yt-dlp (cached locally).
  2. Whisper-transcribe with word-level timestamps.
  3. Pull the FIRST 3 SECONDS of spoken words → the "opener pattern".
  4. Detect cuts/pacing via ffprobe scene-change analysis.
  5. (Music + visual style require additional models; flagged as future
     work but the hooks are in place.)

Output is merged into `campaigns.competitor_insights` alongside the
metadata-only fields. The advisor surfaces "winners open with phrases
like X, Y, Z" + "pacing averages ~Ks per cut" into the scorer prompt.

Intentionally separate from the lightweight learner because:
- Downloads are slow (multi-minute per campaign).
- Whisper transcription is expensive on CPU.
- This is a nightly batch job, not an inline call.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from engine.transcriber import Transcriber


DEEP_DIR = settings.project_root / "data" / "competitor_clips"
# Deeper learn: 12 competitor clips per campaign downloaded + Whisper
# transcribed + ffprobe pacing analyzed. ~25-40 min per campaign on CPU.
# Chris OK'd the time investment for higher-quality competitor signal.
MAX_PERFORMERS_DEEP = 12
HOOK_WINDOW_SEC = 3.0


def refresh_deep_insights(
    repo: Repository,
    campaign_id: Optional[int] = None,
    max_per_campaign: int = MAX_PERFORMERS_DEEP,
) -> dict[int, dict]:
    """Heavy nightly: download + transcribe top performers, extract opener +
    pacing patterns, merge into competitor_insights."""
    DEEP_DIR.mkdir(parents=True, exist_ok=True)

    if campaign_id is not None:
        ids = [campaign_id]
    else:
        with repo.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM campaigns "
                "WHERE top_performers IS NOT NULL AND top_performers != ''"
            ).fetchall()]

    out: dict[int, dict] = {}
    transcriber = Transcriber()
    for cid in ids:
        deep = _deep_for_campaign(repo, cid, transcriber, max_per_campaign)
        if not deep:
            continue
        _merge_into_insights(repo, cid, deep)
        out[cid] = deep
        logger.info(
            f"[deep-comp] #{cid}: openers={deep.get('opener_phrases')[:3] if deep.get('opener_phrases') else 'none'}, "
            f"avg_cuts/sec={deep.get('avg_cuts_per_sec')}"
        )
    return out


def _deep_for_campaign(
    repo: Repository,
    cid: int,
    transcriber: Transcriber,
    max_n: int,
) -> Optional[dict]:
    with repo.conn() as c:
        row = c.execute(
            "SELECT top_performers FROM campaigns WHERE id=?", (cid,)
        ).fetchone()
    if not row or not row["top_performers"]:
        return None
    try:
        performers = json.loads(row["top_performers"])
    except json.JSONDecodeError:
        return None

    camp_dir = DEEP_DIR / f"camp_{cid}"
    camp_dir.mkdir(parents=True, exist_ok=True)

    openers: list[str] = []
    cut_rates: list[float] = []
    analyzed = 0

    for tp in performers[:max_n]:
        if not isinstance(tp, dict):
            continue
        url = tp.get("url")
        if not url:
            continue
        try:
            video_path = _download(url, camp_dir)
        except Exception as e:
            logger.warning(f"[deep-comp] download failed for {url}: {e}")
            continue
        if not video_path or not video_path.exists():
            continue

        # First-3-sec opener phrase
        try:
            segments = transcriber.transcribe(video_path)
            opener = _extract_opener(segments)
            if opener:
                openers.append(opener)
        except Exception as e:
            logger.warning(f"[deep-comp] transcribe failed for {video_path.name}: {e}")

        # Pacing via ffprobe scene-change
        try:
            cut_rate = _measure_cut_rate(video_path)
            if cut_rate is not None:
                cut_rates.append(cut_rate)
        except Exception as e:
            logger.warning(f"[deep-comp] pacing measure failed for {video_path.name}: {e}")

        analyzed += 1

    if analyzed == 0:
        return None

    # Tokenize openers into 2-3 word phrases and pick the most common shape.
    opener_phrases = _common_opener_phrases(openers, top_n=5)

    return {
        "n_deep_analyzed": analyzed,
        "opener_phrases": opener_phrases,
        "opener_full_examples": openers[:3],
        "avg_cuts_per_sec": round(statistics.mean(cut_rates), 3) if cut_rates else None,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------------
def _download(url: str, out_dir: Path) -> Optional[Path]:
    """yt-dlp to mp4 in `out_dir`. Returns the local path or None."""
    out_template = str(out_dir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--format", "mp4",
        "--max-filesize", "50M",
        "-o", out_template,
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return None
    # Find the most recently created mp4 in out_dir.
    candidates = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _extract_opener(segments) -> Optional[str]:
    """Return spoken words within the first HOOK_WINDOW_SEC seconds."""
    words: list[str] = []
    for seg in segments:
        start = getattr(seg, "start", None) or seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0)
        text = getattr(seg, "text", None) or seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")
        if start is None:
            continue
        if start > HOOK_WINDOW_SEC:
            break
        words.append(text.strip())
    return " ".join(words).strip() or None


def _measure_cut_rate(video_path: Path) -> Optional[float]:
    """Approx cuts/second via ffprobe scene-change detection."""
    cmd = [
        settings.ffprobe_path,
        "-loglevel", "error",
        "-select_streams", "v:0",
        "-show_entries", "frame=pkt_pts_time",
        "-of", "csv=p=0",
        "-f", "lavfi",
        f"movie={str(video_path).replace(chr(92), '/')},select=gt(scene\\,0.4)",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    cuts = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    n_cuts = len(cuts)
    # Get duration to normalize.
    dur_cmd = [
        settings.ffprobe_path, "-loglevel", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", str(video_path),
    ]
    try:
        d = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    try:
        duration = float(d.stdout.strip())
    except ValueError:
        return None
    if duration <= 0:
        return None
    return n_cuts / duration


def _common_opener_phrases(openers: list[str], top_n: int = 5) -> list[str]:
    """Most frequent 2-3 word openers across all winning clips."""
    phrases: list[str] = []
    for o in openers:
        words = re.findall(r"\w+", o.lower())
        if len(words) >= 3:
            phrases.append(" ".join(words[:3]))
        elif len(words) >= 2:
            phrases.append(" ".join(words[:2]))
        elif words:
            phrases.append(words[0])
    counts = Counter(phrases)
    return [p for p, _ in counts.most_common(top_n)]


def _merge_into_insights(repo: Repository, cid: int, deep: dict) -> None:
    """Merge `deep` into the existing competitor_insights JSON without
    clobbering the metadata-only fields."""
    with repo.conn() as c:
        row = c.execute(
            "SELECT competitor_insights FROM campaigns WHERE id=?", (cid,)
        ).fetchone()
    existing: dict = {}
    if row and row["competitor_insights"]:
        try:
            existing = json.loads(row["competitor_insights"])
        except json.JSONDecodeError:
            pass
    existing.update(deep)
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET competitor_insights = ? WHERE id = ?",
            (json.dumps(existing), cid),
        )
