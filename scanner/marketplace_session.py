"""Generic Playwright session for web-based clipping marketplaces.

Subclasses provide:
  - login_url, marketplace_url, expected_logged_in_url_hints
  - _fill_login_form(page, email, password)  optional custom fill flow

Same shape as `scanner/whop_login.py`: headed first run, cached
storage_state at `.auth/<platform>.json` after that.

Used by:
  - VyroSession (Vyro)
  - ClipStakeSession
  - ClipAffiliatesSession
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
    TimeoutError as PWTimeout,
    sync_playwright,
)

from config import settings


AUTH_DIR = settings.project_root / ".auth"


class SessionNeedsRefreshError(RuntimeError):
    """Raised by background-safe sessions when their cached state is stale.
    Scheduled jobs catch this and skip cleanly rather than opening a Chrome
    window that interrupts the user."""


class MarketplaceSession:
    """Generic web marketplace session base. Override class vars per platform."""

    platform: str = "marketplace"
    login_url: str = ""
    marketplace_url: str = ""
    logged_in_url_hints: tuple[str, ...] = ()
    login_url_hints: tuple[str, ...] = ("/login", "/signin", "/auth", "/sign-in")
    email_selectors: tuple[str, ...] = (
        'input[type="email"]',
        'input[name="email"]',
        'input[id="email"]',
        'input[autocomplete="email"]',
        'input[name="username"]',
    )
    password_selectors: tuple[str, ...] = (
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    )
    submit_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("Continue")',
        'button:has-text("Login")',
    )

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        headless: Optional[bool] = None,
        login_wait_seconds: int = 300,
        allow_interactive_login: bool = True,
    ) -> None:
        self.email = email
        self.password = password
        self.auth_file = AUTH_DIR / f"{self.platform}.json"
        if headless is None:
            headless = self.auth_file.exists()
        self.headless = headless
        self.login_wait_seconds = login_wait_seconds
        # When False, the session REFUSES to open a headed Chrome — used by
        # scheduled background jobs so they fail gracefully instead of
        # interrupting Chris with surprise login windows.
        self.allow_interactive_login = allow_interactive_login

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def __enter__(self) -> "MarketplaceSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if not self.email or not self.password:
            raise RuntimeError(f"{self.platform}: email/password missing")
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)

        if self.auth_file.exists():
            logger.info(f"[{self.platform}] loading cached session from {self.auth_file}")
            self._context = self._browser.new_context(storage_state=str(self.auth_file))
            self._page = self._context.new_page()
            if self.is_logged_in():
                logger.info(f"[{self.platform}] cached session valid")
                return
            logger.warning(f"[{self.platform}] cached session invalid → re-login")
            self._context.close()

        if not self.allow_interactive_login:
            # Scheduled background jobs land here when the cached session
            # expires. Refuse to pop a headed Chrome at Chris and fail
            # cleanly. The script can choose to Telegram-notify once.
            raise SessionNeedsRefreshError(
                f"{self.platform} session expired — needs interactive re-login"
            )

        if self.headless:
            logger.warning(f"[{self.platform}] forcing headed for first login")
            self._browser.close()
            self._browser = self._pw.chromium.launch(headless=False)

        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._interactive_login()
        self._context.storage_state(path=str(self.auth_file))
        logger.info(f"[{self.platform}] session saved to {self.auth_file}")

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._browser:
                self._browser.close()
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

    def screenshot_campaign_card(self, title_substring: str, out_path: Path) -> Optional[Path]:
        """Click into the new campaign's card on the marketplace home and
        screenshot the detail popup so Chris sees CPM, rules, source URL
        — not his existing content. Closes the popup when done."""
        try:
            self.page.goto(
                self.marketplace_url or self.login_url,
                wait_until="domcontentloaded", timeout=30_000,
            )
            time.sleep(2.5)
            # Click the card whose visible text includes the title substring.
            clicked = self.page.evaluate(
                """(needle) => {
                    const norm = (s) => (s || "").toLowerCase();
                    const cards = document.querySelectorAll(
                        "article, section, [class*='card'], [class*='Card'], li"
                    );
                    for (const c of cards) {
                        if (norm(c.innerText).includes(norm(needle))) {
                            const r = c.getBoundingClientRect();
                            if (r.width < 100 || r.height < 50) continue;
                            c.scrollIntoView({block: 'center'});
                            c.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                title_substring,
            )
            if not clicked:
                logger.warning(f"[{self.platform}] campaign card '{title_substring}' not found")
                return None
            time.sleep(2.5)  # let popup animate in
            out_path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(out_path), full_page=True)
            # Close the popup so the page state is clean for the next iteration.
            for close_sel in (
                'button:has-text("Close")',
                'button[aria-label="Close"]',
                '[data-testid="close"]',
                'svg[aria-label="Close"]',
            ):
                try:
                    cb = self.page.locator(close_sel).first
                    if cb.count() > 0 and cb.is_visible(timeout=800):
                        cb.click(timeout=1500)
                        break
                except Exception:
                    continue
            # Last resort: press Escape.
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return out_path
        except Exception as e:
            logger.warning(f"[{self.platform}] campaign card screenshot failed: {e}")
            return None

    def is_logged_in(self) -> bool:
        try:
            target = self.marketplace_url or self.login_url
            self.page.goto(target, wait_until="domcontentloaded", timeout=20_000)
            url = self.page.url
            if any(h in url for h in self.login_url_hints):
                return False
            if self.logged_in_url_hints and not any(h in url for h in self.logged_in_url_hints):
                # If we have explicit hints, require a match.
                return False
            return True
        except Exception:
            return False

    def _interactive_login(self) -> None:
        logger.info(f"[{self.platform}] navigating to {self.login_url}")
        self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=30_000)
        self._fill_login_form(self.page, self.email, self.password)
        # If a verification-code step appears within the next ~30s, try to
        # auto-fetch the code from the burner email and paste it.
        try:
            self._auto_fill_email_code_if_prompted()
        except Exception as e:
            logger.warning(f"[{self.platform}] email auto-code skipped: {e}")
        print(
            "\n"
            f"============================================================\n"
            f" {self.platform.upper()}: finish login in the open browser.\n"
            f" Complete any 2FA / CAPTCHA. I'll detect the redirect.\n"
            f" Up to {self.login_wait_seconds}s.\n"
            f"============================================================\n",
            flush=True,
        )
        deadline = time.time() + self.login_wait_seconds
        while time.time() < deadline:
            url = self.page.url or ""
            if url and url != "about:blank" and not any(h in url for h in self.login_url_hints):
                logger.info(f"[{self.platform}] login detected at {url}")
                return
            time.sleep(2.0)
        raise RuntimeError(f"{self.platform}: login timed out")

    # ------------------------------------------------------------------
    def _fill_login_form(self, page: Page, email: str, password: str) -> None:
        """Default fill: try each selector list in order. Subclasses can override."""
        self._try_fill(page, self.email_selectors, email)
        self._try_fill(page, self.password_selectors, password)
        # Slight delay before submit so the form's react state catches up.
        time.sleep(0.4)
        self._try_click(page, self.submit_selectors)

    def _auto_fill_email_code_if_prompted(self, wait_seconds: int = 30) -> None:
        """If the page now shows a 'verification code' input within `wait_seconds`,
        poll the burner inbox for a fresh code from this platform and paste it."""
        import time as _t
        from config import settings as _settings
        # Gmail OAuth path — require client secret + cached token both present.
        token_p = _settings.project_root / ".auth" / "gmail-token.json"
        if not (_settings.gmail_client_secret_path and token_p.exists()):
            return  # no Gmail integration set up

        # Selectors that commonly indicate a code-input step.
        code_selectors = (
            'input[name*="code" i]',
            'input[placeholder*="code" i]',
            'input[autocomplete="one-time-code"]',
            'input[name*="verification" i]',
            'input[aria-label*="code" i]',
        )
        deadline = _t.time() + wait_seconds
        code_input = None
        while _t.time() < deadline:
            for sel in code_selectors:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=800):
                        code_input = loc
                        break
                except Exception:
                    continue
            if code_input:
                break
            _t.sleep(1)

        if not code_input:
            return  # no code step appeared — normal sign-in flow

        from engine.email_fetcher import wait_for_code
        logger.info(f"[{self.platform}] code input detected — polling email for code")
        code = wait_for_code(
            sender_contains=self.platform,
            timeout_seconds=90,
        )
        if not code:
            logger.warning(f"[{self.platform}] couldn't fetch code from email")
            return
        try:
            code_input.fill(code, timeout=3000)
            logger.info(f"[{self.platform}] auto-filled code from email")
            # Try to submit
            self._try_click(self.page, self.submit_selectors)
        except Exception as e:
            logger.warning(f"[{self.platform}] auto-fill code failed: {e}")

    @staticmethod
    def _try_fill(page: Page, selectors: tuple[str, ...], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(value, timeout=3_000)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _try_click(page: Page, selectors: tuple[str, ...]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1_500):
                    loc.click(timeout=3_000)
                    return True
            except Exception:
                continue
        return False
