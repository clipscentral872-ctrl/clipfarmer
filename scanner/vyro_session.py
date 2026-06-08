"""Vyro Clips marketplace session + campaign scraper.

Vyro at https://app.vyro.com/ is a web dashboard where you connect your
socials and the platform auto-tracks views/earnings. Login is email +
password (same as Discord burner per Chris's setup).

We:
  1. Log in.
  2. Scrape the marketplace page (https://app.vyro.com/marketplace or
     similar — the script discovers the actual list page).
  3. For each campaign card, extract: name, CPM, source video URL,
     required hashtags / mentions, platforms.
  4. Upsert to the local DB with marketplace='vyro'.

Submission flow on Vyro is *passive* — the platform tracks via the
connected socials, so we don't need a submitter (just need to post).
That's a major simplification vs Whop.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from loguru import logger

from config import settings
from scanner.marketplace_session import MarketplaceSession


class VyroSession(MarketplaceSession):
    platform = "vyro"
    login_url = "https://app.vyro.com/login"
    # Vyro's "Active campaigns" list lives on the Home tab at the root URL,
    # NOT /marketplace (confirmed by Chris's screenshot 2026-06-05).
    marketplace_url = "https://app.vyro.com/"
    logged_in_url_hints = ("app.vyro.com",)  # any post-login path is fine
    login_url_hints = ("/login", "/signin", "/sign-in")

    def __init__(self, **kwargs) -> None:
        super().__init__(
            email=kwargs.pop("email", None) or settings.vyro_email,
            password=kwargs.pop("password", None) or settings.vyro_password,
            **kwargs,
        )

    def _fill_login_form(self, page, email: str, password: str) -> None:
        """Vyro's real flow (confirmed via Chris's screenshots 2026-06-05):
          1. Type email into the single email input
          2. Click 'Continue'
          3. Page switches to 6-digit OTP entry; Vyro emails a code
          4. Poll the burner Gmail for that code (subject 'NNN NNN is your
             verification code' / body 'Your Vyro verification code is: NNNNNN')
          5. Type the 6 digits into the OTP boxes — the browser auto-advances

        No Google OAuth, no password.  `password` arg is ignored; we keep the
        signature so the base class can call us the same way.
        """
        # The page is React-rendered, so the input isn't in the DOM at the
        # instant goto() returns.  Wait up to 15s for any input to appear.
        try:
            page.wait_for_selector(
                "input:visible, input:not([type=hidden])",
                timeout=15_000,
            )
        except Exception:
            logger.warning("[vyro] no input ever appeared after 15s")
            return

        # 1. Email — try a wide list of candidates and pick the first visible one
        email_selectors = (
            'input[type="email"]',
            'input[placeholder="Email" i]',
            'input[placeholder*="email" i]',
            'input[name="email"]',
            'input[autocomplete="email"]',
            'input[autocomplete="username"]',
            'input[type="text"]:visible',
            'input:not([type=hidden]):not([type=submit]):not([type=button]):visible',
        )
        filled = False
        for sel in email_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.wait_for(state="visible", timeout=3000)
                loc.fill(email, timeout=3000)
                logger.info(f"[vyro] filled email via selector: {sel}")
                filled = True
                break
            except Exception as e:
                logger.debug(f"[vyro] selector {sel!r} skipped: {e}")
                continue
        if not filled:
            logger.warning("[vyro] couldn't find email input after waiting")
            return
        time.sleep(0.8)

        # 2. Continue button.  WARNING: the page ALSO has a "Continue with
        # Google" button.  Substring matching would grab the wrong one, so we
        # use exact-text first.  We also wait for it to be enabled (Vyro
        # greys it out until the email passes client-side validation).
        clicked = False
        for sel in (
            'button:text-is("Continue")',
            'button:has-text("Continue"):not(:has-text("Google"))',
            'button[type="submit"]:not(:has-text("Google"))',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.wait_for(state="visible", timeout=3000)
                # Wait for it to become enabled (max 5s)
                for _ in range(10):
                    try:
                        if loc.is_enabled(timeout=500):
                            break
                    except Exception:
                        pass
                    time.sleep(0.5)
                loc.click(timeout=3000)
                logger.info(f"[vyro] clicked Continue via selector: {sel}")
                clicked = True
                break
            except Exception as e:
                logger.debug(f"[vyro] continue selector {sel!r} skipped: {e}")
                continue
        if not clicked:
            logger.warning("[vyro] couldn't find/click Continue button")
            return

        # Capture the click time so we ignore any older verification email
        # (Vyro emails a fresh 6-digit code on every Continue press).
        code_requested_at = time.time()

        # 3. Wait for the OTP entry page to render (or for the URL to change)
        if not self._wait_for_otp_step(page, timeout_sec=20):
            try:
                cur_url = page.url
                inp_count = page.locator('input').count()
                body_text = page.locator('body').inner_text(timeout=2000) or ""
                logger.warning(
                    f"[vyro] OTP step didn't appear. url={cur_url} "
                    f"inputs={inp_count} body='{body_text[:200]}'"
                )
            except Exception as e:
                logger.warning(f"[vyro] OTP step didn't appear; diag failed: {e}")
            return

        # 4. Poll Gmail for the *fresh* code (not_before guards against stale)
        from engine.email_fetcher import wait_for_code
        logger.info("[vyro] OTP step rendered — polling Gmail for code")
        code = wait_for_code(
            sender_contains="vyro",
            timeout_seconds=90,
            not_before=code_requested_at,
        )
        if not code:
            logger.warning("[vyro] no fresh code received from Gmail within 90s")
            return
        logger.info(f"[vyro] received code from email: {code}")

        # 5. Type the code — keyboard.type is the most reliable across both
        # single-input and split-OTP-box renderings.
        if not self._fill_otp_code(page, code):
            logger.warning("[vyro] failed to type OTP code")
            return
        logger.info("[vyro] OTP entered — base class will poll for redirect")

    def _wait_for_otp_step(self, page, timeout_sec: int = 20) -> bool:
        """Return True once the OTP entry view is rendered.  Vyro's UI
        shows the heading 'We emailed you a code' — that text is the most
        reliable anchor since the underlying input markup varies."""
        body_text_re = re.compile(
            r"emailed.*code|enter.*code|verification.*code",
            re.IGNORECASE | re.DOTALL,
        )
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                body_text = ""
                try:
                    body_text = page.locator('body').inner_text(timeout=1500) or ""
                except Exception:
                    pass
                if body_text_re.search(body_text):
                    return True
                # Secondary signals
                if page.locator('input[maxlength="1"]').count() >= 4:
                    return True
                if page.locator('input[autocomplete="one-time-code"]').count() > 0:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _fill_otp_code(self, page, code: str) -> bool:
        """Vyro's OTP boxes may render as 6 split inputs OR a single masked
        input.  Keyboard typing handles both: clicking the first input then
        sending one digit at a time lets the component (or browser autofill)
        route each keystroke into the correct box.  Vyro auto-submits once
        all 6 digits are entered."""
        code = (code or "").strip()
        if not code.isdigit():
            return False
        # Primary: focus the first visible input and type digit-by-digit.
        try:
            first_input = page.locator('input:visible').first
            if first_input.count() == 0:
                first_input = page.locator('input').first
            first_input.click(timeout=2500)
            page.keyboard.type(code, delay=80)
            logger.info(f"[vyro] typed {len(code)}-digit code via keyboard")
            return True
        except Exception as e:
            logger.warning(f"[vyro] keyboard OTP fill failed: {e}")
        # Fallback A: per-box fill if it really is 6 separate inputs
        try:
            otp_inputs = page.locator('input[maxlength="1"]')
            if otp_inputs.count() >= len(code):
                for i, ch in enumerate(code):
                    otp_inputs.nth(i).fill(ch, timeout=1500)
                return True
        except Exception:
            pass
        # Fallback B: single-input fill of the whole code
        try:
            page.locator('input').last.fill(code, timeout=2000)
            return True
        except Exception as e:
            logger.warning(f"[vyro] all OTP fill strategies failed: {e}")
            return False

    # ------------------------------------------------------------------
    def scrape_campaigns(self, limit: int = 50) -> list[dict]:
        """Walk the marketplace listing and pull campaign cards."""
        page = self.page
        try:
            page.goto(self.marketplace_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.warning(f"[vyro] marketplace nav failed: {e}")
            return []
        time.sleep(3)
        # Vyro's marketplace is a list of cards. Use a generic card extractor
        # that doesn't depend on knowing the exact class names — find any
        # element with both a heading + a "CPM" / "$" / "per 1k" text.
        cards = page.evaluate(_CARD_HARVEST_JS)
        out: list[dict] = []
        for c in cards[:limit]:
            entry = _parse_card(c)
            if entry:
                out.append(entry)
        logger.info(f"[vyro] scraped {len(out)} marketplace campaign(s)")
        return out

    def find_campaign(self, name_contains: str) -> Optional[dict]:
        """Find a single campaign whose card text contains `name_contains`."""
        for c in self.scrape_campaigns(limit=100):
            if name_contains.lower() in (c.get("title") or "").lower():
                return c
        return None

    def scrape_campaign_detail(self, campaign_url: str) -> Optional[dict]:
        """Open a campaign's detail page and extract rules + source video."""
        page = self.page
        try:
            page.goto(campaign_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.warning(f"[vyro] detail nav failed: {e}")
            return None
        time.sleep(3)
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5_000) or ""
        except Exception:
            pass
        return {
            "url": campaign_url,
            "full_text": body[:8000],
            "source_links": _extract_source_links(body),
            "hashtags": _extract_hashtags(body),
            "mentions": _extract_mentions(body),
        }


# ----------------------------------------------------------------------
# Vyro's "Active campaigns" cards.  Confirmed structure (2026-06-05):
#   <card>
#     <Brand>             e.g. "Circle"  / "Dhar Mann Studios"
#     <meta>              e.g. "9h ago • Per view"
#     <source title>      e.g. "The Making Of Podcast: Amy Porterfield"
#     <platform icons>    IG / Shorts / TikTok
#     <price>             e.g. "$2,500"
#     <unit>              "PER 1M VIEWS"
#   </card>
# Strategy: find every node whose visible text contains "PER 1M VIEWS",
# walk up to the smallest reasonable card-sized container, then parse.
# ----------------------------------------------------------------------
_CARD_HARVEST_JS = r"""
(() => {
    const out = [];
    const seen = new Set();
    const anchor = /per\s*1\s*m\s*views/i;
    document.querySelectorAll("*").forEach(el => {
        const text = (el.textContent || "").trim();
        if (text.length > 60) return;
        if (!anchor.test(text)) return;
        // Walk up to find the card container (width > ~250, height > ~150)
        let card = el;
        for (let i = 0; i < 10 && card; i++) {
            const r = card.getBoundingClientRect();
            if (r.width >= 250 && r.height >= 150 && r.height <= 600) break;
            card = card.parentElement;
        }
        if (!card || seen.has(card)) return;
        // Skip if parent of multiple cards (i.e. the grid container)
        if (card.querySelectorAll("*").length > 400) return;
        seen.add(card);
        const cardText = (card.innerText || "").trim();
        if (cardText.length < 20) return;
        const link = card.querySelector("a[href]");
        out.push({ text: cardText, href: link ? link.href : null });
    });
    return out;
})()
"""


_META_PATTERNS = (
    re.compile(r"^\d+\s*[hdmw]\s*ago", re.IGNORECASE),
    re.compile(r"^per\s+view$", re.IGNORECASE),
    re.compile(r"^per\s*1\s*m\s*views$", re.IGNORECASE),
    re.compile(r"^active campaigns$", re.IGNORECASE),
    re.compile(r"^select a campaign", re.IGNORECASE),
)


def _is_meta_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("$"):
        return True
    if any(p.search(s) for p in _META_PATTERNS):
        return True
    # bullet-only metadata like "9h ago • Per view"
    if "•" in s and len(s) < 40:
        return True
    return False


def _parse_card(c: dict) -> Optional[dict]:
    text = (c.get("text") or "").strip()
    if not text:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    content = [l for l in lines if not _is_meta_line(l) and len(l) >= 2]
    if not content:
        return None
    brand = content[0][:200]
    source_title = content[1][:200] if len(content) > 1 else None
    cpm = _extract_cpm(text)
    return {
        "title": brand,
        "url": c.get("href"),
        "cpm_usd": cpm,
        "source_title": source_title,
        "raw_text": text,
    }


def _extract_cpm(text: str) -> Optional[float]:
    """Vyro displays '$X,XXX PER 1M VIEWS'.  Convert to per-1k for consistency
    with our other marketplaces (Whop / ClipStake use per-1k)."""
    # Per-1M format (Vyro)
    m = re.search(
        r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*PER\s*1\s*M\s*VIEWS",
        text, re.IGNORECASE,
    )
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
            return round(amount / 1000.0, 4)  # per-1M → per-1k
        except ValueError:
            pass
    # Per-1k / CPM format (fallback)
    m = re.search(
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:per\s*1\s*[kK]|/\s*1\s*[kK]|CPM)",
        text, re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _extract_source_links(text: str) -> list[str]:
    patterns = [
        r"https?://(?:www\.)?youtube\.com/(?:watch|shorts|c/|@|channel/|playlist)[^\s\"'<>]+",
        r"https?://youtu\.be/[^\s\"'<>]+",
        r"https?://(?:www\.)?twitch\.tv/[^\s\"'<>]+",
        r"https?://(?:www\.)?tiktok\.com/[^\s\"'<>]+",
        r"https?://drive\.google\.com/[^\s\"'<>]+",
    ]
    found = []
    for p in patterns:
        found.extend(re.findall(p, text, flags=re.IGNORECASE))
    return list(dict.fromkeys(found))


def _extract_hashtags(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"#\w+", text)))[:20]


def _extract_mentions(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@[A-Za-z0-9_.]+", text)))[:20]
