"""Search YouTube / TikTok / Instagram via the actual app interfaces
(Playwright, not yt-dlp), so we capture what users actually see — including
the platform's algorithmic ranking, current view counts, and recent
trending clips.

- YouTube: public web search, no login needed. ✅ works immediately.
- TikTok:  needs the cached TikTok PlatformSession (login). Falls back to
           public search but TikTok blocks unauthenticated heavy scraping.
- Instagram: needs an IG PlatformSession (login). IG's hashtag pages
             require auth or they redirect to login.

Each returns the same dict shape used by the existing competitor pipeline:
    {url, title, views, platform, source: "social_search_app"}
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import sync_playwright


# Deeper scan: 20 results per platform. With YT+TT+IG that's up to 60
# candidates per campaign feeding into deep_competitor. Chris explicitly
# OK'd longer runs in exchange for better-informed scoring.
PER_PLATFORM_LIMIT = 20


# ----------------------------------------------------------------------
# YouTube — public search, no login
# ----------------------------------------------------------------------
def search_youtube_app(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """Open youtube.com/results, scrape Shorts results sorted by views."""
    # Filter: Shorts only + sort by view count descending.
    # The `sp=` parameter encodes filters. CAMSAhAB encodes "Sort by view count"
    # in our experience; we also try the "Short" filter via the URL #shorts
    # fragment. If filters fail we just take top results.
    base = (
        "https://www.youtube.com/results"
        f"?search_query={query.replace(' ', '+')}+%23shorts"
    )
    results: list[dict] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.locator('button:has-text("Accept all")').first.click(timeout=2_000)
            except Exception:
                pass
            time.sleep(2.5)
            # Scroll to load more results.
            for _ in range(3):
                page.mouse.wheel(0, 3000)
                time.sleep(0.6)

            raw = page.evaluate(_YT_HARVEST_JS)
            browser.close()
        for r in raw[: n * 2]:
            url = r.get("url") or ""
            if not url.startswith("http"):
                url = "https://www.youtube.com" + url
            views = _parse_views(r.get("views_text") or "")
            results.append({
                "url": url,
                "title": (r.get("title") or "")[:200],
                "views": views or 0,
                "channel": (r.get("channel") or "").strip().lower(),
                "duration_sec": r.get("duration_sec") or 0,
                "platform": "youtube",
                "source": "social_search_app",
            })
        results.sort(key=lambda r: r.get("views") or 0, reverse=True)
        return results[:n]
    except Exception as e:
        logger.warning(f"[social-app][yt] failed: {e}")
        return []


_YT_HARVEST_JS = r"""
(() => {
    // YouTube renders search results in many overlapping component types.
    // The metadata layout (where view counts live) differs per type, so we
    // search broadly across all of them, then extract the first VIEW-COUNT
    // token (matching K/M/B) from EITHER aria-label OR visible text.
    const VIEWS_RE = /([0-9]+(?:[.,][0-9]+)?\s*(?:[KkMmBb])?)\s*views?/i;
    // Duration like "0:45", "12:30", "1:02:30"
    const DUR_RE = /\b(\d+):(\d{2})(?::(\d{2}))?\b/;
    const out = [];
    const seen = new Set();
    document.querySelectorAll(
        "ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer, " +
        "ytd-reel-item-renderer, ytd-rich-grid-media, ytd-compact-video-renderer, " +
        "ytm-rich-item-renderer, ytm-video-renderer"
    ).forEach(el => {
        const a = el.querySelector("a[href*='/watch'], a[href*='/shorts/']");
        if (!a) return;
        const href = a.getAttribute("href") || a.href || "";
        if (!href || seen.has(href)) return;
        seen.add(href);

        // Title — many possible holders
        const titleEl = el.querySelector(
            "#video-title, [id='video-title'], yt-formatted-string#video-title, " +
            "h3 a, a#video-title-link, span[role='text']"
        );
        let title = "";
        if (titleEl) {
            title = (titleEl.getAttribute("title") || titleEl.innerText || "").trim();
        }

        // View count — try aria-label first (always has "X views" phrasing),
        // then scan all visible text in the card for the same pattern.
        let views_text = "";
        const ariaSource = el.querySelector("a[aria-label], #video-title[aria-label]");
        if (ariaSource) {
            const al = ariaSource.getAttribute("aria-label") || "";
            const m = al.match(VIEWS_RE);
            if (m) views_text = m[0];
        }
        if (!views_text) {
            const cardText = (el.innerText || "");
            const m = cardText.match(VIEWS_RE);
            if (m) views_text = m[0];
        }

        // Channel name — for filtering out the campaign owner's own clips
        let channel = "";
        const chEl = el.querySelector(
            "ytd-channel-name a, .ytd-channel-name a, yt-formatted-string.ytd-channel-name, " +
            "a[href*='/@'], a[href*='/channel/']"
        );
        if (chEl) {
            channel = (chEl.innerText || chEl.getAttribute("href") || "").trim();
        }

        // Duration — filter out long-form (we want clipper-style shorts).
        // Shorts cards don't always have a visible duration; default 0.
        let duration_sec = 0;
        const durEl = el.querySelector(
            "span.ytd-thumbnail-overlay-time-status-renderer, " +
            "ytd-thumbnail-overlay-time-status-renderer span, " +
            "div.badge-shape-wiz__text"
        );
        if (durEl) {
            const m = (durEl.innerText || "").match(DUR_RE);
            if (m) {
                const h = m[3] ? parseInt(m[1]) : 0;
                const mn = m[3] ? parseInt(m[2]) : parseInt(m[1]);
                const s = m[3] ? parseInt(m[3]) : parseInt(m[2]);
                duration_sec = h * 3600 + mn * 60 + s;
            }
        }

        out.push({
            url: href,
            title: title.slice(0, 200),
            views_text: views_text,
            channel: channel,
            duration_sec: duration_sec,
        });
    });
    return out;
})()
"""


# ----------------------------------------------------------------------
# TikTok — needs cached PlatformSession (signed-in)
# ----------------------------------------------------------------------
def search_tiktok_app(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """Open TikTok search inside the cached profile. If profile isn't
    logged in, returns [] — never triggers interactive login. We don't
    want a background scraper to halt a script waiting for human auth."""
    from config import settings as _s
    profile_dir = _s.project_root / ".auth" / "tiktok-profile"
    if not profile_dir.exists() or not any(profile_dir.iterdir()):
        logger.info("[social-app][tt] no cached profile; skipping (run scripts/platform_login.py tiktok to set up)")
        return []
    from publisher.web_base import PlatformSession
    sess = PlatformSession(
        platform="tiktok",
        login_url="https://www.tiktok.com/login",
        logged_in_url_hints=("/foryou", "/profile"),
        headless=True,
    )
    try:
        sess.start()
        # Cheap session-validity check — if cookies missing, skip cleanly.
        cookies = sess.context.cookies()
        if not any(c["name"] in ("sessionid", "sid_tt") for c in cookies):
            logger.info("[social-app][tt] no session cookie; skipping")
            return []
        url = f"https://www.tiktok.com/search?q={query.replace(' ', '%20')}"
        sess.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(4)
        for _ in range(2):
            sess.page.mouse.wheel(0, 2500)
            time.sleep(0.8)
        raw = sess.page.evaluate(_TT_HARVEST_JS)
        out = []
        for r in raw[: n * 2]:
            views = _parse_views(r.get("views_text") or "")
            url2 = r.get("url") or ""
            if not url2.startswith("http"):
                url2 = "https://www.tiktok.com" + url2
            out.append({
                "url": url2,
                "title": (r.get("title") or "")[:200],
                "views": views or 0,
                "platform": "tiktok",
                "source": "social_search_app",
            })
        out.sort(key=lambda r: r.get("views") or 0, reverse=True)
        return out[:n]
    except Exception as e:
        logger.warning(f"[social-app][tt] failed: {e}")
        return []
    finally:
        try:
            sess.close()
        except Exception:
            pass


_TT_HARVEST_JS = r"""
(() => {
    const out = [];
    document.querySelectorAll(
        "a[href*='/video/'], div[data-e2e='search_video-item']"
    ).forEach(el => {
        const a = el.tagName === 'A' ? el : el.querySelector("a[href*='/video/']");
        if (!a) return;
        const titleEl = el.querySelector("[data-e2e='search-card-desc'], div[class*='caption']");
        const viewsEl = el.querySelector(
            "[data-e2e='video-views'], strong[class*='video-count'], span[class*='video-count']"
        );
        out.push({
            url: a.getAttribute("href") || a.href || "",
            title: titleEl ? (titleEl.innerText || "").trim() : (a.innerText || "").trim().slice(0, 200),
            views_text: viewsEl ? (viewsEl.innerText || "").trim() : "",
        });
    });
    return out;
})()
"""


# ----------------------------------------------------------------------
# Instagram — needs Playwright session (IG Graph API doesn't expose search)
# ----------------------------------------------------------------------
def search_instagram_app(query: str, n: int = PER_PLATFORM_LIMIT) -> list[dict]:
    """Open IG explore tag inside the cached IG PlatformSession. Bails
    early if no cached profile (never triggers interactive login)."""
    from config import settings as _s
    profile_dir = _s.project_root / ".auth" / "instagram-profile"
    if not profile_dir.exists() or not any(profile_dir.iterdir()):
        logger.info("[social-app][ig] no cached profile; skipping (Graph API token alone isn't enough — need Playwright login too)")
        return []
    from publisher.web_base import PlatformSession
    sess = PlatformSession(
        platform="instagram",
        login_url="https://www.instagram.com/accounts/login/",
        logged_in_url_hints=("instagram.com/?", "instagram.com/feed", "/explore"),
        headless=True,
    )
    try:
        sess.start()
        cookies = sess.context.cookies()
        if not any(c["name"] == "sessionid" for c in cookies):
            logger.info("[social-app][ig] no session cookie; skipping")
            return []
        tag = _to_hashtag(query)
        if not tag:
            return []
        url = f"https://www.instagram.com/explore/tags/{tag}/"
        sess.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(4)
        for _ in range(2):
            sess.page.mouse.wheel(0, 2500)
            time.sleep(0.8)
        raw = sess.page.evaluate(_IG_HARVEST_JS)
        out = []
        for r in raw[: n * 2]:
            url2 = r.get("url") or ""
            if url2 and not url2.startswith("http"):
                url2 = "https://www.instagram.com" + url2
            out.append({
                "url": url2,
                "title": (r.get("title") or "")[:200],
                "views": _parse_views(r.get("views_text") or "") or 0,
                "platform": "instagram",
                "source": "social_search_app",
            })
        out.sort(key=lambda r: r.get("views") or 0, reverse=True)
        return out[:n]
    except Exception as e:
        logger.warning(f"[social-app][ig] failed: {e}")
        return []
    finally:
        try:
            sess.close()
        except Exception:
            pass


_IG_HARVEST_JS = r"""
(() => {
    const out = [];
    document.querySelectorAll("a[href*='/p/'], a[href*='/reel/']").forEach(a => {
        const img = a.querySelector("img");
        const title = img ? (img.alt || "") : "";
        out.push({
            url: a.getAttribute("href") || "",
            title: title.slice(0, 200),
            views_text: "",
        });
    });
    return out;
})()
"""


# ----------------------------------------------------------------------
def _parse_views(s: str) -> Optional[int]:
    if not s:
        return None
    s = s.lower().replace(",", "").strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmb]?)", s)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(m.group(2), 1)
    return int(n * mult)


def _to_hashtag(query: str) -> str:
    words = re.findall(r"\w+", query.lower())
    return "".join(words[:3]) if words else ""


def search_for_campaign(query: str) -> list[dict]:
    """All three platforms, merged."""
    yt = search_youtube_app(query)
    tt = search_tiktok_app(query)
    ig = search_instagram_app(query)
    return yt + tt + ig
