"""Auto-fill and submit the Whop Content Rewards submission form.

Reuses the proven direct-apps.whop.com navigation pattern from
`brief_fetcher` + `top_performers_scraper`:
  1. Warm up via whop.com/joined/<community>/<exp>/app/ so cookies land.
  2. Goto apps.whop.com/.../browse-campaigns directly.
  3. Handle "Session Expired" reload.
  4. Wait for .campaign-card-bg, kill the Join-Our-Community overlay.

Then:
  5. Click the program card matching the campaign title.
  6. (Optional) Click into a named sub-campaign.
  7. Click "Submit Video".
  8. Heuristically find form fields (title / video link / demographics image)
     and fill them.
  9. Click the form's submit button.
 10. Dump HTML + screenshot at every step into data/debug/submit/.

We never assume any specific selector — every step tries a list of
selectors and label-text lookups. If a step misses, the script saves the
DOM so the user can paste it back and we tighten heuristics. With
`--dry-run` the script stops just before clicking the final submit, so
you can confirm the fields are filled correctly.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from config import settings
from .brief_fetcher import COMMUNITY_NAV, DEFAULT_COMMUNITY


CARD_SELECTOR = ".campaign-card-bg"
DEBUG_DIR = settings.project_root / "data" / "debug" / "submit"


@dataclass
class SubmissionInputs:
    title: str
    video_url: str
    demographics_image: Optional[Path] = None


@dataclass
class SubmissionResult:
    ok: bool
    message: str = ""
    debug_html: Optional[Path] = None


class WhopSubmitter:
    def __init__(self, page: Page, debug: bool = True) -> None:
        self.page = page
        self.debug = debug
        if debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def submit(
        self,
        *,
        program_title: str,
        sub_campaign_title: Optional[str],
        inputs: SubmissionInputs,
        community_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> SubmissionResult:
        if not self._navigate_to_apps(community_id or DEFAULT_COMMUNITY):
            return SubmissionResult(ok=False, message="couldn't reach apps.whop.com browse-campaigns")

        program_card = self._find_card_by_title(program_title)
        if not program_card:
            self._dump("no_program_card", program_title)
            return SubmissionResult(ok=False, message=f"no program card titled {program_title!r}")
        logger.info(f"[submit] clicking program {program_title!r}")
        self._click_card(program_card)
        time.sleep(10)
        self._dump("after_program_click", program_title)

        if sub_campaign_title:
            sub_card = self._find_card_by_title(sub_campaign_title, partial=True)
            if not sub_card:
                self._dump("no_sub_card", sub_campaign_title)
                return SubmissionResult(ok=False, message=f"no sub-campaign titled {sub_campaign_title!r}")
            logger.info(f"[submit] clicking sub-campaign {sub_campaign_title!r}")
            self._click_card(sub_card)
            time.sleep(10)
            self._dump("after_subcampaign_click", sub_campaign_title)

        # Click the orange "Submit Video" button.
        if not self._click_submit_video_button():
            self._dump("no_submit_video_btn", program_title)
            return SubmissionResult(ok=False, message="couldn't find 'Submit Video' button")
        time.sleep(6)
        self._dump("after_submit_video_btn", program_title)

        # Check for "Verify Platform" modal — Whop blocks submission until each
        # platform (YouTube / Instagram / etc.) is verified for this community
        # by pasting a one-time code into the channel's bio / description.
        ver = self._detect_verification_required()
        if ver is not None:
            self._dump("verification_required", program_title)
            return SubmissionResult(
                ok=False,
                message=(
                    f"Whop needs platform verification before this community will accept submissions. "
                    f"Code: {ver.get('code') or '(could not read from page)'}. "
                    f"Paste it in your {ver.get('platform', 'platform')} {'channel description' if ver.get('platform') == 'youtube' else 'bio'}, "
                    f"save, then trigger the clip again."
                ),
            )

        # Fill form fields.
        filled = self._fill_form(inputs)
        self._dump("after_fill", program_title)
        if not filled.ok:
            return filled

        if dry_run:
            logger.info("[submit] dry-run: stopping before final submit")
            return SubmissionResult(ok=True, message="dry-run: form filled, NOT submitted")

        # Click the final submit on the form.
        if not self._click_form_submit():
            self._dump("no_form_submit", program_title)
            return SubmissionResult(ok=False, message="couldn't find form submit button")
        time.sleep(6)
        self._dump("after_form_submit", program_title)

        # Confirm by looking for a success indicator.
        if _success_signal(self.page):
            return SubmissionResult(ok=True, message="submitted")
        return SubmissionResult(
            ok=False,
            message="form submitted but no success signal found — check data/debug/submit/after_form_submit.html",
        )

    # ------------------------------------------------------------------
    # Navigation (same pattern as brief_fetcher)
    # ------------------------------------------------------------------
    def _navigate_to_apps(self, community_id: str) -> bool:
        nav = COMMUNITY_NAV.get(community_id)
        if not nav:
            logger.error(f"[submit] no COMMUNITY_NAV for {community_id!r}")
            return False
        warm = nav["warm_url"]
        apps = nav["apps_url"]
        # Retry up to 3 times — the apps.whop.com subdomain frequently shows
        # "Session Expired" even when the whop.com session is fresh, and one
        # Reload-Page click sometimes isn't enough to recover. Re-warming via
        # the parent whop.com URL each iteration refreshes the apps cookies.
        for attempt in range(3):
            logger.info(f"[submit] warm-up attempt {attempt + 1}/3: {warm}")
            self.page.goto(warm, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(10)
            logger.info(f"[submit] direct: {apps}")
            self.page.goto(apps, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(8)
            self._handle_session_expired()
            try:
                self.page.wait_for_selector(CARD_SELECTOR, timeout=20_000)
                self._kill_overlay()
                return True
            except PWTimeout:
                logger.warning(f"[submit] cards never rendered on attempt {attempt + 1}; retrying")
                continue
        self._dump("no_cards_after_retries", "")
        return False

    def _handle_session_expired(self) -> None:
        try:
            body = (self.page.locator("body").inner_text(timeout=3_000) or "").lower()
        except Exception:
            return
        if "session expired" in body or "reload page" in body:
            logger.warning("[submit] session expired — clicking Reload Page")
            try:
                self.page.locator('button:has-text("Reload Page")').first.click(timeout=5_000)
                time.sleep(8)
            except Exception as e:
                logger.warning(f"[submit] reload click failed: {e}")

    def _kill_overlay(self) -> None:
        self.page.evaluate("""
            (() => {
                document.querySelectorAll('div, section').forEach(el => {
                    const cs = window.getComputedStyle(el);
                    if ((cs.position === 'fixed' || cs.position === 'sticky')
                        && el.offsetWidth > 200 && el.offsetHeight > 100
                        && el.innerText
                        && /join our community|join now/i.test(el.innerText)) {
                        el.style.display = 'none';
                    }
                });
            })();
        """)
        time.sleep(0.5)

    def _find_card_by_title(self, title: str, partial: bool = False) -> Optional[Locator]:
        cards = self.page.locator(CARD_SELECTOR)
        n = cards.count()
        for i in range(n):
            card = cards.nth(i)
            try:
                h3 = card.locator("h3").first
                if h3.count() == 0:
                    continue
                t = (h3.inner_text(timeout=1_000) or "").strip()
            except Exception:
                continue
            if partial:
                if title.lower() in t.lower():
                    return card
            else:
                if t.lower() == title.lower():
                    return card
        return None

    def _click_card(self, card: Locator) -> None:
        try:
            card.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        try:
            card.click(timeout=5_000, force=True)
        except Exception:
            try:
                card.dispatch_event("click")
            except Exception as e:
                logger.warning(f"[submit] card click failed: {e}")

    # ------------------------------------------------------------------
    # Submit-Video button + form filling
    # ------------------------------------------------------------------
    def _click_submit_video_button(self) -> bool:
        for sel in (
            'button:has-text("Submit Video")',
            'a:has-text("Submit Video")',
            '[role="button"]:has-text("Submit Video")',
            'button:has-text("Submit a Video")',
            'button:has-text("Submit")',
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() == 0:
                    continue
                if not btn.is_visible(timeout=1_500):
                    continue
                logger.info(f"[submit] clicking via {sel}")
                btn.click(timeout=5_000)
                return True
            except Exception:
                continue
        return False

    def _fill_form(self, inputs: SubmissionInputs) -> SubmissionResult:
        # Whop's form uses React controlled inputs. Fill via Playwright's
        # `.fill()` which dispatches the right input + change events. After
        # each field we read back the live value to confirm React state
        # picked it up — otherwise the submit button stays disabled and we
        # waste 6 seconds polling for nothing.

        # Title: <input name="title" placeholder="Enter video title" type="text">
        if not self._fill_react_input(
            'input[name="title"]', inputs.title, label="Title",
        ):
            return SubmissionResult(ok=False, message="couldn't fill Title field")

        # Video Link: placeholder is "https://youtube.com/watch?v=…". No explicit
        # name attribute we can trust, so use placeholder-based selector then
        # fall back to "the visible text input that doesn't have a value yet".
        link_selectors = [
            'input[placeholder*="youtube.com" i]',
            'input[placeholder*="tiktok.com" i]',
            'input[placeholder*="watch?v" i]',
            'input[name="link"]',
            'input[name="videoUrl"]',
            'input[name="url"]',
            'input[type="url"]',
        ]
        if not self._fill_react_input_any(link_selectors, inputs.video_url, label="Link"):
            return SubmissionResult(ok=False, message="couldn't fill Video Link field")

        # Demographics screenshot — explicit selector matching the dumped DOM.
        if inputs.demographics_image and inputs.demographics_image.exists():
            if not self._set_file_input(inputs.demographics_image):
                return SubmissionResult(
                    ok=False,
                    message="couldn't attach demographics image — form will stay invalid",
                )
        else:
            logger.warning("[submit] no demographics image provided — form likely needs one")

        # Acknowledgement checkbox.
        self._tick_acknowledgement()

        return SubmissionResult(ok=True)

    # ------------------------------------------------------------------
    def _fill_react_input(self, selector: str, value: str, *, label: str) -> bool:
        try:
            inp = self.page.locator(selector).first
            if inp.count() == 0:
                logger.warning(f"[submit] {label}: selector {selector} matched 0 elements")
                return False
            inp.click(timeout=2_000)
            inp.fill("", timeout=2_000)
            inp.type(value, delay=20, timeout=5_000)
            # Blur so React's onBlur validators fire.
            self.page.evaluate("(el) => el.blur()", inp.element_handle())
            got = inp.input_value(timeout=1_000)
            if got != value:
                logger.warning(f"[submit] {label}: filled but value mismatch (got {got!r})")
                # Sometimes React strips whitespace etc. — accept partial match.
                if value not in got and got not in value:
                    return False
            logger.info(f"[submit] filled {label}: {value[:60]}…")
            return True
        except Exception as e:
            logger.warning(f"[submit] {label} fill via {selector} failed: {e}")
            return False

    def _fill_react_input_any(self, selectors: list[str], value: str, *, label: str) -> bool:
        for sel in selectors:
            try:
                inp = self.page.locator(sel).first
                if inp.count() == 0:
                    continue
                if self._fill_react_input(sel, value, label=label):
                    return True
            except Exception:
                continue
        return False

    def _set_file_input(self, path: Path) -> bool:
        # The actual file input is `<input type="file" class="hidden" ...>`.
        # Playwright handles hidden inputs fine; we just need to also fire a
        # change event so React's controlled-form state picks it up.
        for sel in (
            'input[type="file"][accept*="image"]',
            'input[type="file"]',
        ):
            try:
                inp = self.page.locator(sel).first
                if inp.count() == 0:
                    continue
                inp.set_input_files(str(path), timeout=5_000)
                # Fire change event explicitly — react-dropzone-style components
                # sometimes only register the file when this dispatches.
                try:
                    inp.evaluate(
                        "(el) => el.dispatchEvent(new Event('change', { bubbles: true }))"
                    )
                except Exception:
                    pass
                # Give React a beat to update its state.
                self.page.wait_for_timeout(500)
                logger.info(f"[submit] uploaded demographics image via {sel}")
                return True
            except Exception as e:
                logger.warning(f"[submit] file input set failed via {sel}: {e}")
                continue
        return False

    def _detect_verification_required(self) -> Optional[dict]:
        """If Whop is showing the 'Verify Platform' / 'Connect Account' flow,
        return {code, platform, username}. Otherwise None."""
        try:
            body = (self.page.locator("body").inner_text(timeout=2_500) or "")
        except Exception:
            return None
        body_l = body.lower()
        if not ("verify platform" in body_l
                or "verify account" in body_l
                or "verification code" in body_l
                or "connect account" in body_l):
            return None

        # Detect which platform Whop is asking us to verify.
        platform = None
        if "youtube" in body_l:
            platform = "youtube"
        elif "instagram" in body_l:
            platform = "instagram"
        elif "tiktok" in body_l:
            platform = "tiktok"
        elif "facebook" in body_l:
            platform = "facebook"
        elif " x " in body_l or "twitter" in body_l:
            platform = "x"

        # Extract the verification code. It's usually shown in a styled box
        # with a copy icon; could be an <input value="ABC..."> or a <code>/<span>
        # element. Try a few candidate selectors.
        code: Optional[str] = None
        for sel in (
            'input[readonly]',
            'input[type="text"][value]',
            'code',
            'div:has-text("Verification code") + div input',
        ):
            try:
                el = self.page.locator(sel).first
                if el.count() == 0:
                    continue
                val = el.input_value(timeout=1_000) if "input" in sel else el.inner_text(timeout=1_000)
                val = (val or "").strip()
                # Whop codes look like 6-12 uppercase alphanumerics
                if re.fullmatch(r"[A-Z0-9]{6,12}", val):
                    code = val
                    break
            except Exception:
                continue
        # Last-resort regex over the visible text
        if not code:
            m = re.search(r"\b([A-Z0-9]{6,12})\b", body)
            if m:
                code = m.group(1)

        # Username is whatever was pre-filled in the connect-account input
        username: Optional[str] = None
        try:
            el = self.page.locator('input[placeholder*="username" i]').first
            if el.count() > 0:
                username = (el.input_value(timeout=1_000) or "").strip() or None
        except Exception:
            pass

        logger.warning(
            f"[submit] Whop verification required — platform={platform} code={code} username={username}"
        )
        return {"code": code, "platform": platform, "username": username}

    def _tick_acknowledgement(self) -> None:
        # Whop uses a Radix-style "sr-only peer" input with a peer-styled label
        # that LOOKS like a checkbox. A normal `.click()` on the label often
        # doesn't toggle the underlying input. Use Playwright's `.check()`
        # which is purpose-built for visually-hidden inputs and falls back to
        # JS-level state toggling when needed.
        for sel in (
            'input#agreement',                 # observed in Whop's form
            'input[name="agreementChecked"]',
            'input[type="checkbox"]',
            '[role="checkbox"]',
        ):
            try:
                cb = self.page.locator(sel).first
                if cb.count() == 0:
                    continue
                try:
                    if cb.is_checked():
                        return
                except Exception:
                    pass
                cb.check(timeout=3_000, force=True)
                logger.info(f"[submit] ticked acknowledgement via {sel}")
                # Give the form a beat to re-validate and enable the submit button.
                self.page.wait_for_timeout(400)
                return
            except Exception:
                continue
        # Last-resort: click the visible label by text.
        try:
            label = self.page.locator(
                'label:has-text("I\'ve read the submission requirements")'
            ).first
            if label.count() > 0 and label.is_visible(timeout=1_500):
                label.click(timeout=3_000)
                self.page.wait_for_timeout(400)
                logger.info("[submit] clicked acknowledgement label (fallback)")
        except Exception:
            logger.warning("[submit] could not tick acknowledgement checkbox")

    def _click_form_submit(self) -> bool:
        # The submit button stays `disabled` until ALL required fields pass
        # Whop's client-side validation (Title, Link, demographics image, and
        # the acknowledgement checkbox). After we tick the box the form
        # re-validates and the button enables a beat later — poll for that
        # transition rather than failing on the first read.
        candidates = (
            'button:has-text("Submit for approval")',
            'button:has-text("Submit for Approval")',
            'button[type="submit"]:not(:has-text("Submit Video"))',
            'button:has-text("Send")',
        )
        deadline = time.time() + 6.0
        last_disabled_log: Optional[str] = None
        while time.time() < deadline:
            for sel in candidates:
                try:
                    btn = self.page.locator(sel).last
                    if btn.count() == 0:
                        continue
                    if not btn.is_visible(timeout=500):
                        continue
                    if not btn.is_enabled(timeout=500):
                        if last_disabled_log != sel:
                            logger.info(f"[submit] {sel} found but disabled; waiting…")
                            last_disabled_log = sel
                        continue
                    logger.info(f"[submit] form submit via {sel}")
                    btn.click(timeout=5_000)
                    return True
                except Exception:
                    continue
            self.page.wait_for_timeout(500)
        logger.warning("[submit] submit button never became enabled within 6s")
        return False

    # ------------------------------------------------------------------
    def _dump(self, label: str, suffix: str) -> None:
        if not self.debug:
            return
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", suffix)[:50] if suffix else ""
        stem = f"{label}__{safe}" if safe else label
        try:
            (DEBUG_DIR / f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[submit] dump html failed: {e}")
        try:
            self.page.screenshot(path=str(DEBUG_DIR / f"{stem}.png"), full_page=True)
        except Exception as e:
            logger.warning(f"[submit] screenshot failed: {e}")


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------
def _fill_by_label_then_attr(
    page: Page,
    *,
    label_re: re.Pattern,
    attr_re: re.Pattern,
    value: str,
) -> bool:
    """Heuristic field filler.

    Tries, in order:
      1. <label>matching</label> → linked input via for=id
      2. input/textarea where name/id/placeholder/aria-label matches attr_re
      3. Any visible single-line text input that's still empty (last resort)
    Returns True on success.
    """
    # 1. label → for=id
    try:
        labels = page.locator("label")
        for i in range(labels.count()):
            l = labels.nth(i)
            try:
                txt = (l.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            if not txt or not label_re.search(txt):
                continue
            for_id = l.get_attribute("for")
            if for_id:
                inp = page.locator(f"#{for_id}").first
                if inp.count() > 0 and inp.is_visible(timeout=500):
                    inp.fill(value, timeout=3_000)
                    return True
            # Or nested input
            nested = l.locator("input, textarea").first
            if nested.count() > 0 and nested.is_visible(timeout=500):
                nested.fill(value, timeout=3_000)
                return True
    except Exception:
        pass

    # 2. attribute match
    try:
        candidates = page.locator("input:not([type='hidden']):not([type='file']), textarea")
        for i in range(candidates.count()):
            inp = candidates.nth(i)
            try:
                if not inp.is_visible(timeout=300):
                    continue
                attrs = " ".join(
                    (inp.get_attribute(a) or "")
                    for a in ("name", "id", "placeholder", "aria-label")
                )
                if not attr_re.search(attrs):
                    continue
                inp.fill(value, timeout=3_000)
                return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _set_first_file_input(page: Page, path: Path) -> bool:
    try:
        finput = page.locator('input[type="file"]').first
        if finput.count() == 0:
            return False
        finput.set_input_files(str(path), timeout=5_000)
        return True
    except Exception as e:
        logger.warning(f"[submit] file input set failed: {e}")
        return False


def _success_signal(page: Page) -> bool:
    """Return True if the page now looks like a success state."""
    try:
        body = (page.locator("body").inner_text(timeout=2_000) or "").lower()
    except Exception:
        return False
    return any(s in body for s in (
        "submitted", "submission received", "thanks for submitting",
        "thank you", "under review", "we've received",
    ))
