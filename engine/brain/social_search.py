"""Search social platforms for what's actually winning on the topic.

For each active campaign, we build a search query from its title (and
the source video's topic) and pull the top-N results sorted by view
count. Those URLs become the campaign's `top_performers` blob — same
schema as the Whop scraper output, so the existing competitor pipeline
(metadata learner + deep_competitor + Director) picks them up
automatically.

Platforms:
  - YouTube: yt-dlp `ytsearch{n}:<query>` works reliably + free. ✅
  - TikTok:  `https://www.tiktok.com/search?q=<query>` requires a logged-in
             session and TikTok aggressively rotates anti-bot. Best-effort
             only — we try, and if extraction fails we log and move on.
  - Instagram: hashtag/search aggressively rate-limits + needs login.
             We try via yt-dlp's IG extractor with hashtag URLs; if it
             returns nothing we skip.

YouTube alone is a strong signal because the same content tends to
cross-post across platforms — the top YT result for "MrBeast Arctic
clips" is usually also a top TikTok / Reels clip. So treat YT as the
primary truth source and the other two as bonus signal when available.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

from loguru import logger

from db.repository import Repository


# Per-platform result cap. Bigger is better for signal but slower for
# the downstream Whisper-based deep_competitor analyzer.
PER_PLATFORM_LIMIT = 8


def search_youtube(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """yt-dlp ytsearchN: returns top N results by relevance/views."""
    cmd = [
        "yt-dlp", "--no-warnings",
        "--skip-download", "--dump-json", "--flat-playlist",
        # ytsearch tends to return relevance-ordered; for view-sorting we
        # use the date filter via a CLI flag, but it's not directly view-
        # sorted by yt-dlp. We post-sort.
        f"ytsearch{n*2}:{query}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"[social-search][yt] failed: {e}")
        return []
    if r.returncode != 0:
        logger.warning(f"[social-search][yt] yt-dlp exit {r.returncode}: {r.stderr[:200]}")
        return []
    results: list[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = d.get("url") or d.get("webpage_url")
        if not url:
            continue
        if url.startswith("http") is False and d.get("id"):
            url = f"https://www.youtube.com/watch?v={d['id']}"
        results.append({
            "url": url,
            "title": (d.get("title") or "")[:200],
            "views": int(d.get("view_count") or 0),
            "duration_sec": d.get("duration"),
            "platform": "youtube",
            "source": "social_search",
        })
    # Sort by view count desc, take top N.
    results.sort(key=lambda r: r.get("views") or 0, reverse=True)
    return results[:n]


def search_tiktok(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """Best-effort TikTok hashtag/text search via yt-dlp."""
    # TikTok search URLs need a logged-in session to return meaningful
    # results; this is a best-effort fallback that often returns nothing.
    url = f"https://www.tiktok.com/search?q={query.replace(' ', '%20')}"
    cmd = [
        "yt-dlp", "--no-warnings",
        "--skip-download", "--dump-json", "--flat-playlist",
        "--playlist-items", f"1-{n}",
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line.strip() or "{}")
        except json.JSONDecodeError:
            continue
        u = d.get("webpage_url") or d.get("url")
        if u and "tiktok.com" in u:
            out.append({
                "url": u,
                "title": (d.get("title") or "")[:200],
                "views": int(d.get("view_count") or 0),
                "platform": "tiktok",
                "source": "social_search",
            })
    out.sort(key=lambda r: r.get("views") or 0, reverse=True)
    return out[:n]


def search_instagram(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """Best-effort IG hashtag search. Often blocked without login."""
    tag = _to_hashtag(query)
    if not tag:
        return []
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    cmd = [
        "yt-dlp", "--no-warnings",
        "--skip-download", "--dump-json", "--flat-playlist",
        "--playlist-items", f"1-{n}",
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line.strip() or "{}")
        except json.JSONDecodeError:
            continue
        u = d.get("webpage_url") or d.get("url")
        if u and "instagram.com" in u:
            out.append({
                "url": u,
                "title": (d.get("title") or "")[:200],
                "views": int(d.get("view_count") or 0),
                "platform": "instagram",
                "source": "social_search",
            })
    out.sort(key=lambda r: r.get("views") or 0, reverse=True)
    return out[:n]


def _to_hashtag(query: str) -> str:
    words = re.findall(r"\w+", query.lower())
    return "".join(words[:3]) if words else ""


# ----------------------------------------------------------------------
def search_for_campaign(repo: Repository, campaign_id: int) -> list[dict]:
    """Build a search query from the campaign title + run all three platforms.

    PRIMARY: Playwright on the actual app interfaces (YT/TT/IG web) —
    gives us what users actually see, including algorithmic rank and
    fresh view counts.

    FALLBACK: yt-dlp search (less reliable on TT/IG since they aggressively
    block scrapers without auth).
    """
    with repo.conn() as c:
        row = c.execute(
            "SELECT title, marketplace_server FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row:
        return []
    query = _build_query(row["title"] or "", row["marketplace_server"] or "")
    if not query:
        return []
    logger.info(f"[social-search] #{campaign_id} query={query!r}")

    # Primary: real-app Playwright scraping
    try:
        from scanner.social_search_app import (
            search_youtube_app, search_tiktok_app, search_instagram_app,
        )
        yt = search_youtube_app(query)
        tt = search_tiktok_app(query)
        ig = search_instagram_app(query)
    except Exception as e:
        logger.warning(f"[social-search] app scraper failed ({e}); falling back to yt-dlp")
        yt, tt, ig = [], [], []

    # Fallback: yt-dlp for any platform that came up empty
    if not yt:
        yt = search_youtube(query)
    if not tt:
        tt = search_tiktok(query)
    if not ig:
        ig = search_instagram(query)

    raw = yt + tt + ig
    # Filter out the campaign owner's own channel + long-form videos.
    # We compare to other CLIPPERS, not to the source creator.
    owners = _owner_handles(row["title"] or "")
    out = []
    skipped_owner = 0
    skipped_long = 0
    for r in raw:
        # Owner channel filter
        ch = (r.get("channel") or r.get("uploader") or r.get("uploader_id") or "").lower()
        url = (r.get("url") or "").lower()
        if owners and (ch in owners or any(o in url for o in owners if o.startswith("@"))):
            skipped_owner += 1
            continue
        # Long-form filter — clipper posts are short
        dur = r.get("duration_sec") or r.get("duration") or 0
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            dur = 0
        if dur and dur > MAX_CLIPPER_DURATION_SEC:
            skipped_long += 1
            continue
        out.append(r)
    logger.info(
        f"[social-search] #{campaign_id}: YT={len(yt)}, TT={len(tt)}, IG={len(ig)} "
        f"→ kept {len(out)} (skipped {skipped_owner} owner, {skipped_long} long-form)"
    )
    return out


def _build_query(title: str, server: str = "") -> str:
    """Turn 'MrBeast (TT/YT) I Survived 7 Days in the Arctic' into
    'MrBeast Arctic clips'. Heuristic but works for most campaigns."""
    # Strip parens groups like (TT/YT) / (IG)
    s = re.sub(r"\([^)]*\)", "", title).strip()
    # Drop generic words.
    drop = {"clipping", "campaign", "official", "studios", "podcast", "ad"}
    words = [w for w in re.findall(r"\w+", s) if w.lower() not in drop]
    if not words:
        words = re.findall(r"\w+", title)
    # Keep the first 5 meaningful words + " clips" so search returns
    # short-form re-cuts not the original long-form video.
    q = " ".join(words[:5])
    if "clips" not in q.lower() and "clip" not in q.lower():
        q = f"{q} clips"
    return q


def _owner_handles(title: str) -> set[str]:
    """Best-effort guess at the campaign owner's channel handles.

    For a title like 'MrBeast (TT/YT) I Survived 7 Days in the Arctic' the
    owner is 'MrBeast' / '@MrBeast' / '@mrbeast'. We filter competitor
    search results that match any of these so we don't compare ourselves
    against the original creator's own posts (which trivially win).
    """
    # First meaningful word before the parenthetical / generic words.
    s = re.sub(r"\([^)]*\)", "", title).strip()
    drop = {"clipping", "campaign", "official", "studios", "podcast", "ad", "the"}
    words = [w for w in re.findall(r"\w+", s) if w.lower() not in drop]
    if not words:
        return set()
    owner = words[0]
    return {
        owner.lower(),
        f"@{owner.lower()}",
        owner.replace(" ", "").lower(),
        f"@{owner.replace(' ', '').lower()}",
    }


# Skip videos longer than this — long-form is the source we're clipping
# FROM, not what other clippers post. Anyone posting a 60-min video to
# YouTube Shorts didn't make a clip.
MAX_CLIPPER_DURATION_SEC = 180


def refresh_social_top_performers(repo: Repository, campaign_id: Optional[int] = None) -> dict[int, int]:
    """For each active campaign (or one if specified), populate top_performers
    from social search results. Returns {campaign_id: count_written}."""
    if campaign_id is not None:
        ids = [campaign_id]
    else:
        with repo.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM campaigns WHERE (status IS NULL OR status='active')"
            ).fetchall()]
    out: dict[int, int] = {}
    for cid in ids:
        results = search_for_campaign(repo, cid)
        if not results:
            continue
        # Merge with whatever's already in top_performers — Whop scraper
        # data + social search data are complementary.
        with repo.conn() as c:
            row = c.execute(
                "SELECT top_performers FROM campaigns WHERE id=?", (cid,)
            ).fetchone()
        existing = []
        if row and row["top_performers"]:
            try:
                existing = json.loads(row["top_performers"])
            except Exception:
                existing = []
        # De-dup by URL.
        seen_urls = {p.get("url") for p in existing if isinstance(p, dict)}
        for r in results:
            if r["url"] not in seen_urls:
                existing.append(r)
                seen_urls.add(r["url"])
        repo.set_campaign_top_performers(cid, existing)
        out[cid] = len(results)
    return out
