"""Fire `/clips add` against Clipify (Sx Bot) on a per-streamer Discord
server using the burner DiscordSession.

Public surface:

    sub = ClipifySubmitter()
    sub.submit_post(
        server_name="Viptoria X Clipify",
        channel_name="commands",
        platform="youtube",
        urls=["https://www.youtube.com/shorts/abc"],
    )

    # or group by platform:
    sub.submit_posts(
        server_name="Viptoria X Clipify",
        channel_name="commands",
        urls_by_platform={
            "youtube": ["https://..."],
            "instagram": ["https://..."],
        },
    )
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from scanner.discord_session import DiscordSession


# Clipify's /clips add accepts these platform values (matches the picker
# autocomplete Chris screenshotted).
PLATFORM_ALIAS = {
    "youtube": "youtube",
    "shorts": "youtube",
    "yt": "youtube",
    "instagram": "instagram",
    "ig": "instagram",
    "reel": "instagram",
    "tiktok": "tiktok",
    "tt": "tiktok",
    "x": "twitter",
    "twitter": "twitter",
}


class ClipifySubmitter:
    """Thin wrapper that owns a DiscordSession and fires /clips add."""

    def __init__(self, session: Optional[DiscordSession] = None) -> None:
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "ClipifySubmitter":
        if self._session is None:
            self._session = DiscordSession()
            self._session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> DiscordSession:
        if self._session is None:
            raise RuntimeError("ClipifySubmitter not started")
        return self._session

    def submit_post(
        self,
        server_name: str,
        channel_name: str,
        platform: str,
        urls: list[str],
    ) -> dict:
        """Fire one `/clips add platform:X urls:URL1 URL2...` and return
        {"ok": bool, "reply": str|None, "error": str|None}."""
        plat = PLATFORM_ALIAS.get((platform or "").lower(), platform)
        if not urls:
            return {"ok": False, "reply": None, "error": "no urls"}
        urls_str = " ".join(urls)
        try:
            self.session.send_slash_command(
                server_name=server_name,
                channel_name=channel_name,
                command="clips add",
                options={"platform": plat, "urls": urls_str},
            )
        except Exception as e:
            logger.error(f"[clipify] send failed: {e}")
            return {"ok": False, "reply": None, "error": str(e)}
        reply = self.session.read_last_bot_reply(bot_name_hint="Clipify")
        ok = bool(reply) and "error" not in (reply or "").lower()
        return {"ok": ok, "reply": reply, "error": None}

    def submit_posts(
        self,
        server_name: str,
        channel_name: str,
        urls_by_platform: dict[str, list[str]],
    ) -> dict[str, dict]:
        """One /clips add per platform group. Returns map platform → result."""
        results: dict[str, dict] = {}
        for plat, urls in urls_by_platform.items():
            if not urls:
                continue
            results[plat] = self.submit_post(
                server_name=server_name,
                channel_name=channel_name,
                platform=plat,
                urls=urls,
            )
        return results
