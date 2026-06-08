"""Shared Playwright session helper for web-based platform publishers.

Each platform (TikTok, YouTube, Instagram) gets its own cached
storage_state under .auth/<platform>.json. First login is interactive
(headed browser, user logs in once). After that, headless reuse.

Mirrors the pattern proven in scanner/whop_login.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from config import settings


AUTH_DIR = settings.project_root / ".auth"


class PlatformSession:
    """Cached Playwright session for one social platform.

    Uses a *persistent* Chrome profile per platform so Google (and others)
    can't detect Playwright as easily. The profile dir under
    `.auth/<platform>-profile/` is reused across runs — once you log in,
    you stay logged in.
    """

    def __init__(
        self,
        platform: str,
        login_url: str,
        logged_in_url_hints: tuple[str, ...],
        login_url_hints: tuple[str, ...] = ("/login", "/signin", "/auth", "accounts/login"),
        headless: Optional[bool] = None,
        login_wait_seconds: int = 600,
    ) -> None:
        self.platform = platform
        self.login_url = login_url
        self.logged_in_url_hints = logged_in_url_hints
        self.login_url_hints = login_url_hints
        self.auth_file = AUTH_DIR / f"{platform}.json"
        self.profile_dir = AUTH_DIR / f"{platform}-profile"
        self.login_wait_seconds = login_wait_seconds
        # If we already have a profile, default to headless. If not, force headed.
        if headless is None:
            headless = self.profile_dir.exists() and any(self.profile_dir.iterdir())
        self.headless = headless

        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def __enter__(self) -> "PlatformSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()

        # If profile dir is empty (first run), force headed so the user
        # can log in.
        profile_empty = not any(self.profile_dir.iterdir())
        headless = False if profile_empty else self.headless

        self._context = self._launch_persistent(headless=headless)
        self._page = self._context.new_page() if not self._context.pages else self._context.pages[0]
        self._apply_stealth(self._page)

        if profile_empty:
            logger.info(f"[{self.platform}] first run — interactive login")
            self._interactive_login()
            logger.info(f"[{self.platform}] profile populated, persists at {self.profile_dir}")
            return

        # We already have a profile. Verify it's still logged in.
        if self.is_logged_in():
            logger.info(f"[{self.platform}] cached profile is logged in")
            return

        logger.warning(f"[{self.platform}] cached profile is logged out — re-running interactive login")
        self._context.close()
        self._context = self._launch_persistent(headless=False)
        self._page = self._context.new_page() if not self._context.pages else self._context.pages[0]
        self._apply_stealth(self._page)
        self._interactive_login()

    def _launch_persistent(self, headless: bool) -> BrowserContext:
        """Launch a persistent Chrome profile. Tries real Chrome first,
        then Edge, then bundled Chromium (which Google blocks)."""
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        kwargs_base = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "args": launch_args,
            "viewport": {"width": 1366, "height": 820},
            "locale": "en-US",
        }
        last_err: Optional[Exception] = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(kwargs_base)
                if channel:
                    kwargs["channel"] = channel
                ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                logger.info(f"[{self.platform}] launched persistent context channel={channel or 'bundled-chromium'}")
                return ctx
            except Exception as e:
                last_err = e
                logger.warning(f"[{self.platform}] channel={channel} failed: {e}")
        raise RuntimeError(f"No working browser channel: {last_err}")

    def _apply_stealth(self, page: Page) -> None:
        """Hide the obvious automation tells (navigator.webdriver, etc.)."""
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """
        )

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError(f"{self.platform} session not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError(f"{self.platform} session not started")
        return self._context

    # ------------------------------------------------------------------
    # Cookie names that prove a real auth session, per platform. URL
    # checks alone aren't enough — TikTok's /foryou and YT's homepage
    # both render for logged-out users.
    _SESSION_COOKIE_NAMES = {
        "tiktok": ("sessionid", "sid_tt", "sid_guard"),
        "instagram": ("sessionid",),
        "youtube": ("SAPISID", "SID"),
        "youtube-studio": ("SAPISID", "SID"),
    }

    def is_logged_in(self) -> bool:
        page = self.page
        try:
            target = self.logged_in_url_hints[0] if self.logged_in_url_hints else self.login_url
            if target.startswith("/"):
                from urllib.parse import urlparse
                u = urlparse(self.login_url)
                target = f"{u.scheme}://{u.netloc}{target}"
            page.goto(target, wait_until="domcontentloaded", timeout=20_000)
            url = page.url
            if any(h in url for h in self.login_url_hints):
                return False
            # Stronger check: a real session sets specific cookies.
            required = self._SESSION_COOKIE_NAMES.get(self.platform)
            if required:
                names = {c["name"] for c in self._context.cookies()}
                if not any(n in names for n in required):
                    logger.info(
                        f"[{self.platform}] URL ok but no session cookie "
                        f"({'/'.join(required)}) — not really logged in"
                    )
                    return False
            return True
        except Exception:
            return False

    def _interactive_login(self) -> None:
        marker = AUTH_DIR / f"{self.platform}-login-complete"
        if marker.exists():
            marker.unlink()
        logger.info(f"[{self.platform}] opening {self.login_url}")
        self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=30_000)
        print(
            "\n"
            "============================================================\n"
            f" Log into {self.platform} in the open browser window.\n"
            f" Once you're on your home feed / dashboard, leave it.\n"
            f" I'll detect the URL change automatically.\n"
            f" If detection misses, I'll drop a marker file and continue.\n"
            f" Up to {self.login_wait_seconds}s.\n"
            "============================================================\n",
            flush=True,
        )

        deadline = time.time() + self.login_wait_seconds
        stable = 0
        last_url = ""
        while time.time() < deadline:
            if marker.exists():
                marker.unlink()
                logger.info(f"[{self.platform}] manual marker triggered")
                return
            try:
                cur = self.page.url
            except Exception:
                cur = ""
            if cur and cur != last_url:
                logger.info(f"[{self.platform}] page: {cur}")
                last_url = cur
            on_login = bool(cur) and any(h in cur for h in self.login_url_hints)
            if cur and not on_login and cur != "about:blank":
                stable += 1
                if stable >= 3:
                    logger.info(f"[{self.platform}] login detected")
                    return
            else:
                stable = 0
            time.sleep(2.0)
        raise RuntimeError(f"Timed out waiting for {self.platform} login")
