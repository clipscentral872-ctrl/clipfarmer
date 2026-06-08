"""Extract source-video URLs that a campaign provides for clipping.

Pulls candidate video URLs from the campaign's stored `rules` text and
from any anchor tags on the live submission_url page. We accept any URL
that looks like a video host yt-dlp can handle (YouTube, Twitch, Kick,
X/Twitter, Vimeo, plus any URL containing 'video' or 'watch').
"""

from __future__ import annotations

import re
from typing import Iterable

from loguru import logger
from playwright.sync_api import TimeoutError as PWTimeout

from db import Repository

from .whop_login import WhopSession


VIDEO_HOST_RE = re.compile(
    r"https?://"
    r"(?:www\.|m\.|mobile\.)?"
    r"("
    r"youtube\.com|youtu\.be|"
    r"twitch\.tv|"
    r"kick\.com|"
    r"vimeo\.com|"
    r"x\.com|twitter\.com|"
    r"tiktok\.com|"
    r"instagram\.com|"
    r"facebook\.com|fb\.watch|"
    r"rumble\.com|"
    r"dailymotion\.com"
    r")"
    r"/[^\s\"'<>]+",
    re.IGNORECASE,
)


class SourceExtractor:
    def __init__(self, session: WhopSession, repo: Repository) -> None:
        self.session = session
        self.repo = repo

    def extract_for_campaign(self, campaign_id: int, campaign_url: str) -> list[int]:
        urls = self._collect_urls(campaign_id, campaign_url)
        ids: list[int] = []
        for url in urls:
            try:
                row_id = self.repo.add_source_video(campaign_id, url)
                ids.append(row_id)
            except Exception as e:
                logger.warning(f"[source] failed to persist {url}: {e}")
        if ids:
            logger.info(f"[source] campaign {campaign_id}: {len(ids)} source video(s)")
        return ids

    def _collect_urls(self, campaign_id: int, campaign_url: str) -> list[str]:
        urls: set[str] = set()

        # 1) Pull URLs from the rules text we already saved on the campaign row.
        with self.repo.conn() as c:
            row = c.execute(
                "SELECT rules FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        if row and row["rules"]:
            urls.update(_extract_from_text(row["rules"]))

        # 2) Visit the campaign page live and pull anchor hrefs.
        if campaign_url:
            try:
                page = self.session.page
                page.goto(campaign_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    pass

                # Body text (rules can be updated since last scan).
                try:
                    body = page.locator("body").inner_text(timeout=2_000) or ""
                    urls.update(_extract_from_text(body))
                except Exception:
                    pass

                # Anchor hrefs.
                for a in page.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                    except Exception:
                        continue
                    if VIDEO_HOST_RE.match(href):
                        urls.add(href)
            except Exception as e:
                logger.warning(f"[source] could not visit {campaign_url}: {e}")

        return sorted(urls)


def _extract_from_text(text: str) -> Iterable[str]:
    return [m.group(0) for m in VIDEO_HOST_RE.finditer(text)]
