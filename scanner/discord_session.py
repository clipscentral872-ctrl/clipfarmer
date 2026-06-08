"""Playwright-driven Discord web session for the burner account.

Same shape as WhopSession: first run is headed so the user can solve
CAPTCHAs / email verification, subsequent runs restore storage_state.

Risks acknowledged elsewhere — this is a burner account by design.

Public surface:
    with DiscordSession() as ds:
        ds.send_slash_command(
            server_name="Viptoria X Clipify",
            channel_name="commands",
            command="clips add",
            options={"platform": "youtube", "urls": "https://... https://..."},
        )
"""

from __future__ import annotations

import re
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
AUTH_FILE = AUTH_DIR / "discord.json"

LOGIN_URL = "https://discord.com/login"
APP_URL = "https://discord.com/channels/@me"

LOGGED_OUT_HINTS = ("/login", "/register")


class DiscordSession:
    """Burner-account Discord web session.

    NOT for the user's main account. Use the burner credentials.
    """

    def __init__(
        self,
        headless: Optional[bool] = None,
        login_wait_seconds: int = 300,
    ) -> None:
        if headless is None:
            headless = AUTH_FILE.exists()
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def __enter__(self) -> "DiscordSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)

        if AUTH_FILE.exists():
            logger.info(f"[discord] loading cached session from {AUTH_FILE}")
            self._context = self._browser.new_context(storage_state=str(AUTH_FILE))
            self._page = self._context.new_page()
            if self.is_logged_in():
                logger.info("[discord] cached session valid")
                return
            logger.warning("[discord] cached session invalid → re-login")
            self._context.close()

        if self.headless:
            logger.warning(
                "[discord] no valid session and headless=True; switching to headed for first login"
            )
            self._browser.close()
            self._browser = self._playwright.chromium.launch(headless=False)

        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._interactive_login()
        self._context.storage_state(path=str(AUTH_FILE))
        logger.info(f"[discord] session saved to {AUTH_FILE}")

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("DiscordSession not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("DiscordSession not started")
        return self._context

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    def is_logged_in(self) -> bool:
        try:
            self.page.goto(APP_URL, wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            return False
        url = self.page.url or ""
        if any(h in url for h in LOGGED_OUT_HINTS):
            return False
        # The app UI shows [aria-label="Servers"] sidebar when logged in.
        try:
            sidebar = self.page.locator('[aria-label="Servers"]').first
            if sidebar.count() > 0 and sidebar.is_visible(timeout=3_000):
                return True
        except PWTimeout:
            pass
        return False

    def _interactive_login(self) -> None:
        email = settings.discord_burner_email
        password = settings.discord_burner_password
        if not email or not password:
            raise RuntimeError(
                "DISCORD_BURNER_EMAIL / DISCORD_BURNER_PASSWORD missing from .env"
            )
        logger.info("[discord] opening login page")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            self.page.locator('input[name="email"]').fill(email, timeout=8_000)
            self.page.locator('input[name="password"]').fill(password, timeout=8_000)
            self.page.locator('button[type="submit"]').click(timeout=5_000)
        except PWTimeout:
            logger.warning("[discord] couldn't auto-fill — finish login manually")

        print(
            "\n"
            "============================================================\n"
            " Complete Discord login in the open browser window.\n"
            " Solve any CAPTCHA / email-code prompts. I'll save the\n"
            f" session once you reach the app. Up to {self.login_wait_seconds}s.\n"
            "============================================================\n",
            flush=True,
        )
        deadline = time.time() + self.login_wait_seconds
        while time.time() < deadline:
            url = self.page.url or ""
            if url and "/channels/" in url and "/login" not in url:
                logger.info(f"[discord] logged in (url={url})")
                return
            time.sleep(2.0)
        raise RuntimeError("Discord login timed out")

    # ------------------------------------------------------------------
    # Server / channel navigation
    # ------------------------------------------------------------------
    def open_server(self, server_name: str) -> None:
        """Click the server icon in the left sidebar matching `server_name`."""
        # Server icons have aria-label="Server name" in the [aria-label="Servers"] nav.
        nav = self.page.locator('[aria-label="Servers"]')
        nav.wait_for(state="visible", timeout=15_000)
        icon = nav.locator(f'[aria-label*="{server_name}"]').first
        if icon.count() == 0:
            raise RuntimeError(
                f"Server '{server_name}' not found in sidebar. Make sure the burner has joined it."
            )
        icon.click(timeout=5_000)
        # Wait for the server's channel list to render.
        self.page.wait_for_selector('[aria-label="Channels"]', timeout=10_000)
        time.sleep(0.8)  # let lazy content settle

    def open_channel(self, channel_name: str) -> None:
        """Click a text channel by name (without the # prefix)."""
        channels = self.page.locator('[aria-label="Channels"]')
        link = channels.locator(f'a[aria-label*="{channel_name}"]').first
        if link.count() == 0:
            # fall back to text match
            link = channels.locator(f'text=/^{re.escape(channel_name)}$/').first
        if link.count() == 0:
            raise RuntimeError(f"Channel '#{channel_name}' not found in current server")
        link.click(timeout=5_000)
        self.page.wait_for_selector('[data-slate-editor="true"]', timeout=10_000)
        time.sleep(0.6)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    def send_slash_command(
        self,
        server_name: str,
        channel_name: str,
        command: str,
        options: Optional[dict[str, str]] = None,
        wait_seconds: float = 6.0,
    ) -> None:
        """Type a slash command in `#channel` of `server_name` and submit it.

        `command` is the command path, e.g. "clips add".
        `options` are parameter name → value pairs that Discord's slash-command
        picker will accept as plain text after the option chip is selected.
        """
        self.open_server(server_name)
        self.open_channel(channel_name)

        editor = self.page.locator('[data-slate-editor="true"]').first
        editor.click(timeout=5_000)
        editor.focus()

        full = "/" + command + " "
        # Slow type so Discord's autocomplete picker reliably catches the command.
        self.page.keyboard.type(full, delay=40)
        time.sleep(0.6)
        # Press Tab/Enter on the autocomplete suggestion to lock in the command.
        self.page.keyboard.press("Tab")
        time.sleep(0.4)

        if options:
            for name, value in options.items():
                # Each option's picker chip is selected by typing the name then Tab.
                self.page.keyboard.type(name, delay=30)
                time.sleep(0.3)
                self.page.keyboard.press("Tab")
                time.sleep(0.3)
                self.page.keyboard.type(value, delay=20)
                time.sleep(0.4)

        # Submit.
        self.page.keyboard.press("Enter")
        # Give Clipify time to respond so we can scrape its reply.
        time.sleep(wait_seconds)
        logger.info(f"[discord] sent /{command} in #{channel_name} ({server_name})")

    def read_last_bot_reply(self, bot_name_hint: str = "Clipify") -> Optional[str]:
        """Best-effort scrape of the most recent message text in the open channel.
        Returns None if nothing found within 5s."""
        try:
            messages = self.page.locator('[id^="chat-messages-"]').all()
            if not messages:
                return None
            last = messages[-1]
            text = last.inner_text(timeout=3_000)
            if bot_name_hint and bot_name_hint.lower() not in text.lower():
                return text  # still return — caller decides
            return text
        except PWTimeout:
            return None
        except Exception as e:
            logger.warning(f"[discord] read_last_bot_reply failed: {e}")
            return None
