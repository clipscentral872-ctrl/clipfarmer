"""Reverse-engineer winning clips on the competitor side.

Input: `campaigns.top_performers` — the JSON blob the Whop top-performers
scraper populates with {url, views, title, ...} for each panel entry.

For each URL we:
  1. Fetch lightweight metadata via yt-dlp (--dump-json, no download) to
     get the public title, description, duration, uploader, view count.
  2. Use that text (title + description) as a pseudo-transcript and run
     it through `style_classifier.classify_clip` to tag a style.
  3. Aggregate per campaign:
        - dominant_styles: top 1-2 styles by count
        - median_duration: typical winning clip length
        - typical_view_count: median views across performers
        - hook_words: most common opening words in titles
  4. Save to `campaigns.competitor_insights` (JSON).

The advisor surfaces those insights to the scorer prompt so Claude
imitates the winning competitor shape.

We deliberately use METADATA-only (no audio download) because:
- top_performers can be 5+ per campaign × 7 campaigns = 35+ downloads
- titles + descriptions are already informative for style + topic
- Heavy version (download + Whisper transcribe) is a separate
  `learn_competitors.py --deep` mode if Chris ever wants it.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from db.repository import Repository
from engine.style_classifier import classify_clip


MIN_TOP_PERFORMERS_PER_CAMPAIGN = 2


def refresh_competitor_insights(
    repo: Repository,
    campaign_id: Optional[int] = None,
) -> dict[int, dict]:
    """Compute insights for one campaign or all. Returns {cid: insights}."""
    _ensure_column(repo)
    if campaign_id is not None:
        ids = [campaign_id]
    else:
        with repo.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM campaigns "
                "WHERE top_performers IS NOT NULL AND top_performers != ''"
            ).fetchall()]
    out: dict[int, dict] = {}
    for cid in ids:
        insights = _compute_for_campaign(repo, cid)
        if not insights:
            continue
        _persist(repo, cid, insights)
        out[cid] = insights
        logger.info(
            f"[competitor] #{cid}: dominant_styles={insights.get('dominant_styles')}, "
            f"median_duration={insights.get('median_duration_sec')}s, "
            f"n={insights.get('n_analyzed')}"
        )
    return out


def get_competitor_insights(repo: Repository, campaign_id: int) -> Optional[dict]:
    _ensure_column(repo)
    with repo.conn() as c:
        row = c.execute(
            "SELECT competitor_insights FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row or not row["competitor_insights"]:
        return None
    try:
        return json.loads(row["competitor_insights"])
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------
def _compute_for_campaign(repo: Repository, campaign_id: int) -> Optional[dict]:
    with repo.conn() as c:
        row = c.execute(
            "SELECT title, top_performers FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row or not row["top_performers"]:
        return None
    try:
        performers = json.loads(row["top_performers"])
    except json.JSONDecodeError:
        return None
    if not isinstance(performers, list) or len(performers) < MIN_TOP_PERFORMERS_PER_CAMPAIGN:
        return None

    styles: list[str] = []
    durations: list[float] = []
    titles: list[str] = []
    view_counts: list[int] = []
    analyzed = 0

    for tp in performers:
        if not isinstance(tp, dict):
            continue
        url = tp.get("url")
        title_hint = tp.get("title") or ""
        views = _parse_views(tp.get("views"))
        if views:
            view_counts.append(views)

        meta = _fetch_metadata(url) if url else None
        if meta:
            title = (meta.get("title") or title_hint or "")[:240]
            description = (meta.get("description") or "")[:1200]
            dur = meta.get("duration")
            if dur:
                durations.append(float(dur))
        else:
            title = title_hint[:240]
            description = ""

        if not title and not description:
            continue
        titles.append(title)
        # Use title + description as the "transcript" for the classifier.
        pseudo_transcript = f"{title}\n\n{description}".strip()
        tag = classify_clip(transcript_excerpt=pseudo_transcript, hook_text=title[:80])
        if tag and tag.get("style"):
            styles.append(tag["style"])
        analyzed += 1
        if analyzed >= 8:  # cap per campaign — avoid burning API tokens
            break

    if analyzed == 0:
        return None

    style_counts = Counter(styles)
    dominant_styles = [{"style": s, "n": n} for s, n in style_counts.most_common(2)]
    hook_words = _common_first_words(titles, top_n=6)

    return {
        "n_analyzed": analyzed,
        "dominant_styles": dominant_styles,
        "median_duration_sec": int(statistics.median(durations)) if durations else None,
        "typical_view_count": int(statistics.median(view_counts)) if view_counts else None,
        "common_hook_words": hook_words,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fetch_metadata(url: str) -> Optional[dict]:
    """yt-dlp --dump-json for metadata only. Returns None on failure."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-warnings", "--skip-download", "--dump-json", url],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"[competitor] yt-dlp failed for {url}: {e}")
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _parse_views(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().replace(",", "")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)$", s, flags=re.IGNORECASE)
    if not m:
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    n = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(n * mult)


def _common_first_words(titles: list[str], top_n: int = 5) -> list[str]:
    """First 2-word phrase of each title; return the most repeated openers."""
    openers: list[str] = []
    for t in titles:
        words = re.findall(r"\w+", (t or "").lower())
        if len(words) >= 2:
            openers.append(" ".join(words[:2]))
        elif words:
            openers.append(words[0])
    counts = Counter(openers)
    return [w for w, _ in counts.most_common(top_n)]


_COLUMN_CHECKED = False


def _ensure_column(repo: Repository) -> None:
    global _COLUMN_CHECKED
    if _COLUMN_CHECKED:
        return
    with repo.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "competitor_insights" not in cols:
            c.execute("ALTER TABLE campaigns ADD COLUMN competitor_insights TEXT")
    _COLUMN_CHECKED = True


def _persist(repo: Repository, campaign_id: int, insights: dict) -> None:
    with repo.conn() as c:
        c.execute(
            "UPDATE campaigns SET competitor_insights = ? WHERE id = ?",
            (json.dumps(insights), campaign_id),
        )
