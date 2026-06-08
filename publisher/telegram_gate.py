"""Telegram human-in-the-loop approval gate.

Before any clip goes live, send a preview (video + caption + hook + the
campaign it targets) to the user's Telegram chat. They reply /approve or
/reject (or tap an inline button). The publisher only proceeds on
explicit /approve.

Uses the bare Telegram Bot HTTP API via `requests` — no third-party SDK.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config import settings


TG_BASE = "https://api.telegram.org"
VERDICT_FILE = Path(__file__).resolve().parent.parent / ".auth" / "verdict.json"


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    DISABLED = "disabled"   # gate disabled by config


@dataclass
class ApprovalResult:
    status: ApprovalStatus
    note: str = ""


class TelegramGate:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.enabled = bool(self.bot_token and self.chat_id and settings.require_telegram_approval)
        # Track the highest update_id we've consumed so we don't re-process old replies.
        self._update_offset = 0
        if self.enabled:
            # Seed the offset to "now" so old chat history doesn't auto-approve anything.
            self._seed_offset()

    # ------------------------------------------------------------------
    def _api(self, method: str, **params) -> dict:
        url = f"{TG_BASE}/bot{self.bot_token}/{method}"
        r = requests.post(url, data=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text}")
        return r.json()

    def _api_send_video(self, video_path: Path, caption: str) -> dict:
        # Telegram's free bot API caps uploads at 50 MB. Above that, sendVideo
        # returns 413 Request Entity Too Large. We fall back to sending the
        # first frame as a photo with the same caption so Chris can still
        # see what he's approving without watching the full video.
        size_mb = video_path.stat().st_size / (1024 * 1024)
        if size_mb >= 48:
            logger.warning(
                f"[telegram] {video_path.name} is {size_mb:.1f} MB (over the 50 MB "
                f"sendVideo limit). Falling back to a thumbnail."
            )
            return self._api_send_thumbnail(video_path, caption)

        url = f"{TG_BASE}/bot{self.bot_token}/sendVideo"
        with video_path.open("rb") as fh:
            r = requests.post(
                url,
                data={
                    "chat_id": self.chat_id,
                    "caption": caption[:1024],   # TG max
                    "parse_mode": "HTML",
                    "supports_streaming": "true",
                },
                files={"video": (video_path.name, fh, "video/mp4")},
                timeout=600,
            )
        if r.status_code == 413:
            logger.warning("[telegram] sendVideo 413 — falling back to thumbnail")
            return self._api_send_thumbnail(video_path, caption)
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram sendVideo failed: {r.status_code} {r.text}")
        return r.json()

    def _api_send_thumbnail(self, video_path: Path, caption: str) -> dict:
        """Extract first-frame thumbnail with ffmpeg, send as a photo."""
        import subprocess
        from config import settings as _settings
        thumb_path = video_path.with_suffix(".thumb.jpg")
        try:
            subprocess.run(
                [
                    _settings.ffmpeg_path or "ffmpeg", "-y",
                    "-ss", "1",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=540:-2",
                    "-q:v", "3",
                    str(thumb_path),
                ],
                capture_output=True, timeout=30, check=False,
            )
        except Exception as e:
            logger.warning(f"[telegram] thumbnail extract failed: {e}")
        url = f"{TG_BASE}/bot{self.bot_token}/sendPhoto"
        body = caption[:1024] + "\n\n<i>(Video too large for Telegram preview — showing thumbnail. Reply /approve or /reject.)</i>"
        if thumb_path.exists():
            with thumb_path.open("rb") as fh:
                r = requests.post(
                    url,
                    data={"chat_id": self.chat_id, "caption": body, "parse_mode": "HTML"},
                    files={"photo": (thumb_path.name, fh, "image/jpeg")},
                    timeout=120,
                )
        else:
            # Last resort: text-only message
            r = requests.post(
                f"{TG_BASE}/bot{self.bot_token}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=60,
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram fallback send failed: {r.status_code} {r.text}")
        return r.json()

    def _seed_offset(self) -> None:
        try:
            data = self._api("getUpdates")
            updates = data.get("result", [])
            if updates:
                self._update_offset = max(u["update_id"] for u in updates) + 1
        except Exception as e:
            logger.warning(f"[telegram] could not seed offset: {e}")

    # ------------------------------------------------------------------
    def send_clip_for_approval(
        self,
        *,
        video_path: Path,
        campaign_title: str,
        campaign_payout: Optional[float],
        hook_text: str,
        caption_text: str,
        hashtags: list[str],
        platforms: list[str],
        structured_rules: Optional[dict] = None,
    ) -> str:
        """Send the preview message + video. Returns the approval token (the message_id).

        When `structured_rules` is provided, the preview also lists the campaign's
        DO/DON'T bullets, forbidden phrases, and notes whether the fixed caption
        was applied — so the human reviewer can sanity-check rule compliance
        before approving.
        """
        full_caption = caption_text.rstrip()
        if hashtags:
            full_caption += "\n\n" + " ".join("#" + h.lstrip("#") for h in hashtags)

        payout_line = f"  ${campaign_payout:.2f}/1k" if campaign_payout else ""
        rules_block = _format_rules_for_approval(structured_rules)
        preview = (
            f"<b>Clip ready for approval</b>\n"
            f"<b>Campaign:</b> {_escape(campaign_title)}{payout_line}\n"
            f"<b>Platforms:</b> {', '.join(platforms)}\n"
            f"<b>Hook:</b> {_escape(hook_text)}\n\n"
            f"<b>Caption:</b>\n{_escape(full_caption)}\n"
            + (f"\n{rules_block}\n" if rules_block else "")
            + "\nReply <b>/approve</b> or <b>/reject</b> to this video."
        )

        self._api_send_video(video_path, preview)
        # Telegram doesn't return a stable correlation id we can poll on,
        # so we just track "the next reply we see" as the verdict.
        # The seeded offset already excludes pre-existing messages.
        token = str(int(time.time()))
        logger.info(f"[telegram] sent clip for approval (token {token})")
        return token

    def wait_for_verdict(self, token: str, timeout_minutes: int = 30) -> ApprovalResult:
        """Wait for /approve or /reject. When the bot daemon is running, it's the
        sole Telegram poller and writes verdicts to a shared file — we read those
        instead of polling Telegram ourselves (avoids offset-stealing race). Falls
        back to polling Telegram directly if no file appears AND we're certain no
        bot is also polling (legacy / standalone runs)."""
        if not self.enabled:
            return ApprovalResult(status=ApprovalStatus.DISABLED)

        # Clear any stale verdict file from a previous run before we start.
        try:
            if VERDICT_FILE.exists():
                VERDICT_FILE.unlink()
        except Exception:
            pass

        deadline = time.time() + timeout_minutes * 60
        # Use file-based verdict by default — robust whether or not a bot is
        # running. If the bot isn't running, polling Telegram is the only path
        # and would deliver the verdict; we'd be silent. So also poll Telegram
        # in parallel and let whichever arrives first win.
        poll_offset = self._update_offset
        while time.time() < deadline:
            # 1. Check the verdict file (bot daemon writes here on /approve, /reject).
            if VERDICT_FILE.exists():
                try:
                    import json as _json
                    data = _json.loads(VERDICT_FILE.read_text(encoding="utf-8"))
                    VERDICT_FILE.unlink()
                    status_str = (data.get("status") or "").upper()
                    if status_str == "APPROVED":
                        logger.info("[telegram] APPROVED (via verdict file)")
                        return ApprovalResult(status=ApprovalStatus.APPROVED)
                    if status_str == "REJECTED":
                        note = data.get("note", "")
                        logger.info(f"[telegram] REJECTED (via verdict file): {note}")
                        return ApprovalResult(status=ApprovalStatus.REJECTED, note=note)
                except Exception as e:
                    logger.warning(f"[telegram] verdict file read failed: {e}")

            # 2. Also poll Telegram directly — works for standalone runs and
            #    gracefully no-ops if the bot already consumed the update.
            try:
                data = self._api("getUpdates", offset=poll_offset, timeout=8)
            except Exception as e:
                logger.debug(f"[telegram] getUpdates error (likely bot also polling): {e}")
                time.sleep(2)
                continue
            for update in data.get("result", []):
                poll_offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message") or {}
                text = (msg.get("text") or "").strip().lower()
                if not text:
                    continue
                if text.startswith("/approve"):
                    logger.info("[telegram] APPROVED (via direct poll)")
                    return ApprovalResult(status=ApprovalStatus.APPROVED)
                if text.startswith("/reject"):
                    note = text.replace("/reject", "", 1).strip()
                    logger.info(f"[telegram] REJECTED (via direct poll): {note}")
                    return ApprovalResult(status=ApprovalStatus.REJECTED, note=note)
            time.sleep(2)
        logger.warning(f"[telegram] approval timed out after {timeout_minutes} min")
        return ApprovalResult(status=ApprovalStatus.TIMED_OUT)

    def notify(self, text: str, *, urgent: bool = False) -> None:
        """Fire-and-forget status notification (e.g. 'posted to YouTube: <url>').

        Outside the configured active window (default 15:00-21:30 SAST), the
        message is queued to .auth/telegram_queue.jsonl and flushed at the next
        active-window start.  Pass `urgent=True` to bypass the gate for
        time-sensitive operational alerts (auth tokens revoked, etc.)."""
        if not self.enabled:
            return
        from .quiet_hours import is_in_active_window, enqueue
        if not urgent and not is_in_active_window():
            enqueue({"kind": "notify", "text": text})
            logger.info(f"[telegram] queued notify (quiet hours): {text[:60]}")
            return
        try:
            self._api("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML", disable_web_page_preview="true")
        except Exception as e:
            logger.warning(f"[telegram] notify failed: {e}")

    def send_photo(self, photo_path: Path, caption: str = "", *, urgent: bool = False) -> None:
        """Send an image with caption. Used by the opportunity scanner to
        attach a screenshot of the marketplace where a new campaign appeared.

        Respects quiet hours like `notify` — pass `urgent=True` to bypass.
        Queued photos store the absolute path; the flusher re-reads the file
        at delivery time so a deleted screenshot becomes a text-only fallback."""
        if not self.enabled:
            return
        from .quiet_hours import is_in_active_window, enqueue
        if not urgent and not is_in_active_window():
            enqueue({
                "kind": "photo",
                "photo_path": str(Path(photo_path).resolve()) if photo_path else None,
                "caption": caption,
            })
            logger.info(f"[telegram] queued photo (quiet hours): {Path(photo_path).name if photo_path else '<no path>'}")
            return
        if not photo_path or not Path(photo_path).exists():
            # Fall back to text-only if the screenshot's missing.
            if caption:
                self.notify(caption, urgent=urgent)
            return
        try:
            url = f"{TG_BASE}/bot{self.bot_token}/sendPhoto"
            with Path(photo_path).open("rb") as fh:
                r = requests.post(
                    url,
                    data={
                        "chat_id": self.chat_id,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"photo": (Path(photo_path).name, fh, "image/jpeg")},
                    timeout=60,
                )
            if r.status_code >= 400:
                logger.warning(f"[telegram] sendPhoto {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"[telegram] send_photo failed: {e}")

    def flush_queue(self) -> int:
        """Deliver every message queued during quiet hours. Called by the
        scheduler at the top of each active window. Returns the count sent."""
        if not self.enabled:
            return 0
        from .quiet_hours import drain_queue
        items = drain_queue()
        if not items:
            return 0
        # Prefix the first message with a digest header so Chris sees at a
        # glance how many pings were held back.
        header = (
            f"<b>📬 {len(items)} message(s) held during quiet hours</b>\n"
            f"<i>Delivering now that the active window has opened.</i>"
        )
        try:
            self._api("sendMessage", chat_id=self.chat_id, text=header,
                      parse_mode="HTML", disable_web_page_preview="true")
        except Exception as e:
            logger.warning(f"[telegram] flush header failed: {e}")
        sent = 0
        for item in items:
            try:
                kind = item.get("kind")
                if kind == "notify":
                    self._api(
                        "sendMessage",
                        chat_id=self.chat_id,
                        text=item.get("text", ""),
                        parse_mode="HTML",
                        disable_web_page_preview="true",
                    )
                    sent += 1
                elif kind == "photo":
                    # Bypass the gate — we're explicitly flushing.
                    p = item.get("photo_path")
                    cap = item.get("caption", "")
                    if p and Path(p).exists():
                        self.send_photo(Path(p), cap, urgent=True)
                    elif cap:
                        self.notify(cap, urgent=True)
                    sent += 1
            except Exception as e:
                logger.warning(f"[telegram] flush failed for one item: {e}")
        logger.info(f"[telegram] flushed {sent}/{len(items)} queued message(s)")
        return sent


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _format_rules_for_approval(rules: Optional[dict]) -> str:
    """Compact human-readable rule summary for the approval message.

    Pre-flight already blocks rule-violating clips before we get here, so
    this block exists to let the human spot-check anything pre-flight
    can't see (e.g. 'does the talent look good?', visual context).
    """
    if not rules:
        return ""
    pieces = ["<b>✅ Rules check (auto pre-flight passed — please eyeball too):</b>"]
    if rules.get("required_caption"):
        pieces.append("• Required caption applied verbatim ✔")
    forbidden = rules.get("forbidden_phrases") or []
    if forbidden:
        pieces.append("• Forbidden phrases (must NOT appear): " + ", ".join(forbidden[:8]))
    donts = rules.get("dont_list") or []
    for d in donts[:6]:
        pieces.append(f"• 🚫 {_escape(d)}")
    dos = rules.get("do_list") or []
    for d in dos[:4]:
        pieces.append(f"• ✅ {_escape(d)}")
    return "\n".join(pieces)
