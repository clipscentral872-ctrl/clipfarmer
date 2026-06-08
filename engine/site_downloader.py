"""Playwright-driven YouTube downloader via a converter site.

When yt-dlp / Cobalt / pytubefix all hit YouTube's SABR + PO Token wall,
we can still drive a real headless browser against one of the consumer
"paste URL → get MP4" sites.  Those sites either run on residential IPs
or solve YouTube's challenges server-side; from our perspective it's just
a click flow.

Implementation tries sites in order, returns on first success.

Notes:
  - We use Playwright Chromium with `--disable-blink-features=AutomationControlled`
    so the sites' anti-headless heuristics don't tank us.
  - We intercept the actual MP4 URL the site emits and stream it directly
    rather than letting Playwright handle the download (more reliable).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from loguru import logger


class SiteDownloadError(RuntimeError):
    pass


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def download_via_site(source_url: str, output_path: Path, timeout: int = 600) -> Path:
    """Try converter sites in priority order; return first success."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SiteDownloadError("playwright not installed") from e

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        mp4_url: Optional[str] = None
        last_err: Optional[Exception] = None
        for site_fn in (_try_savetube, _try_yt5s, _try_ssyoutube):
            try:
                mp4_url = site_fn(page, source_url)
                if mp4_url:
                    logger.info(f"[site] {site_fn.__name__} produced MP4 URL")
                    break
            except Exception as e:
                logger.warning(f"[site] {site_fn.__name__} failed: {e}")
                last_err = e
                continue

        browser.close()

    if not mp4_url:
        raise SiteDownloadError(f"every converter site failed; last error: {last_err!r}")

    logger.info(f"[site] streaming MP4 ({mp4_url[:80]}...) to {output_path.name}")
    with requests.get(mp4_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with output_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    size = output_path.stat().st_size
    if size < 100_000:    # <100 KB usually means we got an error page, not a video
        raise SiteDownloadError(f"downloaded file suspiciously small: {size} bytes")
    logger.info(f"[site] saved {output_path.name} ({size:,} bytes)")
    return output_path


# --------------------------------------------------------------------- sites

def _try_savetube(page, youtube_url: str) -> Optional[str]:
    """savetube.me — Vue.js single-page-app; type URL → click Download → MP4 buttons."""
    page.goto("https://savetube.me/", wait_until="domcontentloaded", timeout=30_000)
    # Input field
    inp = page.locator("input[type='text'], input[type='url']").first
    inp.wait_for(state="visible", timeout=10_000)
    inp.fill(youtube_url)
    # Submit
    for btn_sel in (
        "button:has-text('Download')",
        "button[type='submit']",
        "button.btn-primary",
    ):
        try:
            page.locator(btn_sel).first.click(timeout=5_000)
            break
        except Exception:
            continue
    # Wait for the result links to render
    time.sleep(8)
    # The result UI lists multiple quality buttons; capture any direct .mp4 href
    for sel in (
        "a:has-text('Download MP4')",
        "a[href*='.mp4']",
        "a[download]",
    ):
        try:
            link = page.locator(sel).first
            href = link.get_attribute("href", timeout=3_000)
            if href and ".mp4" in href:
                return href
        except Exception:
            continue
    return None


def _try_yt5s(page, youtube_url: str) -> Optional[str]:
    """yt5s.com — long-running converter, lo-fi UI but works headlessly."""
    page.goto("https://yt5s.com/en", wait_until="domcontentloaded", timeout=30_000)
    inp = page.locator("#s_input, input[name='q']").first
    inp.wait_for(state="visible", timeout=10_000)
    inp.fill(youtube_url)
    page.locator("button:has-text('Start'), #btn_search").first.click(timeout=5_000)
    time.sleep(10)
    # The download buttons appear inside .result table
    for sel in (
        "a:has-text('Get link'):not([disabled])",
        "a[href*='.mp4']",
        "a[download][href]",
    ):
        try:
            href = page.locator(sel).first.get_attribute("href", timeout=3_000)
            if href and "http" in href:
                return href
        except Exception:
            continue
    return None


def _try_ssyoutube(page, youtube_url: str) -> Optional[str]:
    """ssyoutube.com — drops you on savefrom.net's prepopulated downloader.
    Cleanest single-redirect flow when it works."""
    # Insert 'ss' before 'youtube' in the URL
    rewritten = youtube_url.replace("youtube.com", "ssyoutube.com").replace(
        "youtu.be", "ssyoutu.be"
    )
    page.goto(rewritten, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(8)
    for sel in (
        "a.link-download[href*='mp4']",
        "a:has-text('MP4')",
        "a[href*='.mp4']",
    ):
        try:
            href = page.locator(sel).first.get_attribute("href", timeout=3_000)
            if href and "http" in href and "mp4" in href.lower():
                return href
        except Exception:
            continue
    return None
