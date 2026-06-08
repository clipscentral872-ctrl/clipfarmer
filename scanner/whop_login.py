"""Playwright-driven Whop login + persistent session.

Strategy:
  - On first run, open a headed browser, attempt to pre-fill email/password,
    then wait for the user to finish (including any 2FA email code) until
    we detect we're logged in. Save storage_state to .auth/whop.json.
  - On subsequent runs, restore storage_state and verify it's still valid
    by hitting a logged-in-only endpoint. If invalid, fall back to the
    interactive login again.

Designed to be tolerant of Whop UI changes: if a selector misses, the
headed browser still lets the user finish the login manually and the
session still gets saved.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from config import settings

AUTH_DIR = settings.project_root / ".auth"
AUTH_FILE = AUTH_DIR / "whop.json"

LOGIN_URL = "https://whop.com/login"
HOME_URL = "https://whop.com/"

EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[id="email"]',
    'input[autocomplete="email"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[id="password"]',
    'input[autocomplete="current-password"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
]

# These URL fragments mean we've left the login flow.
LOGGED_IN_URL_HINTS = ("/dashboard", "/joined", "/discover", "/home")
LOGIN_URL_HINTS = ("/login", "/signin", "/auth", "/sign-in")


class WhopSession:
    """Wraps a Playwright browser context authenticated to Whop."""

    def __init__(
        self,
        headless: Optional[bool] = None,
        login_wait_seconds: int = 300,
    ) -> None:
        # If we have a cached auth file, default to headless. Otherwise headed.
        if headless is None:
            headless = AUTH_FILE.exists()
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def __enter__(self) -> "WhopSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)

        if AUTH_FILE.exists():
            logger.info(f"[whop] loading cached session from {AUTH_FILE}")
            self._context = self._browser.new_context(storage_state=str(AUTH_FILE))
            self._page = self._context.new_page()
            if self.is_logged_in():
                logger.info("[whop] cached session is valid")
                return
            logger.warning("[whop] cached session expired or invalid, re-login required")
            self._context.close()

        # Either no cache, or cache was invalid → interactive login.
        if self.headless:
            logger.warning(
                "[whop] no valid session and headless=True. "
                "Forcing headed mode for first login so you can complete email-code 2FA."
            )
            self._browser.close()
            self._browser = self._playwright.chromium.launch(headless=False)

        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._interactive_login()
        self._context.storage_state(path=str(AUTH_FILE))
        logger.info(f"[whop] session saved to {AUTH_FILE}")

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("WhopSession not started — call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("WhopSession not started — call start() first.")
        return self._context

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------
    def is_logged_in(self) -> bool:
        """Quick probe: visit home page, look for the login button (means logged out)."""
        try:
            self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            return False

        url = self.page.url
        if any(h in url for h in LOGIN_URL_HINTS):
            return False

        # Whop generally shows a "Log in" button when logged out. Search for it.
        try:
            login_btn = self.page.locator('a:has-text("Log in"), button:has-text("Log in")').first
            if login_btn and login_btn.count() > 0:
                # If it's there and visible, we're not logged in.
                if login_btn.is_visible(timeout=1_000):
                    return False
        except PWTimeout:
            pass
        return True

    def _try_fill(self, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(value, timeout=3_000)
                    return True
            except PWTimeout:
                continue
            except Exception:
                continue
        return False

    def _try_click(self, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1_500):
                    loc.click(timeout=3_000)
                    return True
            except PWTimeout:
                continue
            except Exception:
                continue
        return False

    def _interactive_login(self) -> None:
        email = settings.whop_email
        password = settings.whop_password
        if not email or not password:
            raise RuntimeError(
                "WHOP_EMAIL / WHOP_PASSWORD missing from .env — cannot start login."
            )

        logger.info(f"[whop] navigating to {LOGIN_URL}")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

        # Best-effort pre-fill. If selectors miss, the user can still type by hand.
        filled_email = self._try_fill(EMAIL_SELECTORS, email)
        if filled_email:
            logger.info("[whop] email pre-filled")
            # Some flows reveal password after a "Continue" click.
            self._try_click(SUBMIT_SELECTORS)
            time.sleep(1.5)

        filled_pw = self._try_fill(PASSWORD_SELECTORS, password)
        if filled_pw:
            logger.info("[whop] password pre-filled")
            self._try_click(SUBMIT_SELECTORS)

        if not filled_email or not filled_pw:
            logger.warning(
                "[whop] could not auto-fill all fields — finish login manually in the open browser."
            )

        print(
            "\n"
            "============================================================\n"
            " Complete login in the open browser window.\n"
            " If Whop sends a 2FA code to your email, paste it now.\n"
            " I will detect when you've made it past the login page and\n"
            " save the session automatically. Up to "
            f"{self.login_wait_seconds}s.\n"
            "============================================================\n",
            flush=True,
        )

        # Wait either for: (a) any page in the browser context to leave the
        # login flow, OR (b) the user to drop a `.auth/login-complete` marker
        # file so they can explicitly signal "I'm in" if URL detection misses.
        marker = AUTH_DIR / "login-complete"
        if marker.exists():
            marker.unlink()

        deadline = time.time() + self.login_wait_seconds
        last_logged: dict[str, str] = {}
        while time.time() < deadline:
            # Manual override.
            if marker.exists():
                logger.info("[whop] login-complete marker detected — saving session")
                marker.unlink()
                return

            # Inspect every open page in the context.
            for p in list(self.context.pages):
                try:
                    url = p.url or ""
                except Exception:
                    continue
                pid = str(id(p))
                if url and last_logged.get(pid) != url:
                    logger.info(f"[whop] page: {url}")
                    last_logged[pid] = url

                on_login = any(h in url for h in LOGIN_URL_HINTS) if url else True
                if url and url != "about:blank" and not on_login:
                    logger.info(f"[whop] login detected on page: {url}")
                    return

            time.sleep(2.0)

        raise RuntimeError("Timed out waiting for Whop login to complete.")
