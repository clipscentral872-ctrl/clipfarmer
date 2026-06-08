"""Fetch verification codes from the burner Gmail via the Gmail API.

Uses OAuth 2.0 installed-app flow — same shape as the YouTube publisher.
First-run opens a browser for consent; refresh token cached forever.

Why API instead of IMAP:
- Google has been deprecating App Passwords for newer accounts.
- OAuth refresh tokens auto-renew indefinitely (no human in the loop).
- gmail.readonly scope is minimum-privilege — we can't send or modify.

Public surface (unchanged from prior IMAP version):
    get_latest_code(sender_contains, max_age_seconds=180)
    wait_for_code(sender_contains, timeout_seconds=120)

Credentials needed in .env:
    GMAIL_CLIENT_SECRET_PATH=.auth/gmail-client-secret.json
    GMAIL_TOKEN_PATH=.auth/gmail-token.json   (auto-created)
"""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# Verification-code patterns. Order matters — most specific first.
_CODE_PATTERNS = [
    re.compile(r"\b(?:your )?(?:code|verification code|confirmation code|pin)\s*(?:is|:)?\s*([0-9]{4,8})\b", re.IGNORECASE),
    re.compile(r"\b([0-9]{4,8})\b\s+is\s+your\s+(?:code|verification)", re.IGNORECASE),
    re.compile(r"^\s*([0-9]{6})\s*$", re.MULTILINE),
    re.compile(r"\b([0-9]{4,8})\b"),
]

_MAGIC_LINK_RE = re.compile(
    r"https?://[^\s<>\"']+(?:verify|confirm|magic|sign-?in|login|token)[^\s<>\"']*",
    re.IGNORECASE,
)


def _get_service():
    """Build the Gmail API service. Triggers OAuth flow on first run."""
    client_secret = settings.gmail_client_secret_path
    if not client_secret:
        raise RuntimeError(
            "GMAIL_CLIENT_SECRET_PATH missing in .env — download client_secret JSON "
            "from Google Cloud Console (Gmail API + OAuth Desktop client)"
        )
    client_secret_p = Path(client_secret)
    if not client_secret_p.is_absolute():
        client_secret_p = settings.project_root / client_secret_p
    if not client_secret_p.exists():
        raise RuntimeError(f"Gmail client secret file not found: {client_secret_p}")

    token_p = Path(settings.gmail_token_path or
                   str(settings.project_root / ".auth" / "gmail-token.json"))
    if not token_p.is_absolute():
        token_p = settings.project_root / token_p

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_p.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_p), SCOPES)
        except Exception as e:
            logger.warning(f"[gmail] couldn't load cached token: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("[gmail] refreshing OAuth token")
            creds.refresh(Request())
        else:
            logger.info("[gmail] running OAuth flow — browser will open for consent")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_p), SCOPES)
            creds = flow.run_local_server(port=0)
        token_p.parent.mkdir(parents=True, exist_ok=True)
        token_p.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"[gmail] cached token to {token_p}")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ----------------------------------------------------------------------
def get_latest_code(
    sender_contains: str,
    *,
    max_age_seconds: int = 180,
    body_must_contain: Optional[str] = None,
) -> Optional[str]:
    """Most recent verification code from `sender_contains` within `max_age_seconds`."""
    msg = _find_recent_message(sender_contains, max_age_seconds, body_must_contain)
    if not msg:
        return None
    return _extract_code(msg)


def get_latest_magic_link(
    sender_contains: str,
    *,
    max_age_seconds: int = 180,
) -> Optional[str]:
    msg = _find_recent_message(sender_contains, max_age_seconds)
    if not msg:
        return None
    body = _get_text(msg)
    m = _MAGIC_LINK_RE.search(body or "")
    return m.group(0) if m else None


def wait_for_code(
    sender_contains: str,
    *,
    timeout_seconds: int = 120,
    poll_interval: int = 5,
    body_must_contain: Optional[str] = None,
    not_before: Optional[float] = None,
) -> Optional[str]:
    """Poll until a fresh code arrives from `sender_contains` or timeout.

    `not_before` (unix ts): only accept messages arriving on/after this time.
    Defaults to the moment this call starts.  Use a captured timestamp
    (e.g. the click that triggered the code email) to avoid grabbing a
    stale code from an earlier login attempt.
    """
    if not_before is None:
        not_before = datetime.now(timezone.utc).timestamp()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        msg = _find_recent_message(sender_contains, timeout_seconds + 30, body_must_contain)
        if msg:
            arrived = _msg_unix_ts(msg)
            # 5s skew tolerance — way tighter than the old 30s so we never
            # accept a code from a prior attempt (Vyro / etc. resend on each
            # login, so the OLD code would be wrong even if it parses).
            if arrived and arrived >= not_before - 5:
                code = _extract_code(msg)
                if code:
                    logger.info(f"[gmail] got code from {sender_contains}: {code} (arrived {arrived - not_before:.1f}s after request)")
                    return code
        time.sleep(poll_interval)
    logger.warning(f"[gmail] timeout waiting for code from {sender_contains}")
    return None


# ----------------------------------------------------------------------
def _find_recent_message(
    sender_contains: str,
    max_age_seconds: int,
    body_must_contain: Optional[str] = None,
) -> Optional[dict]:
    """Search Gmail for recent messages from sender. Returns the parsed
    message dict, or None."""
    try:
        svc = _get_service()
    except Exception as e:
        logger.warning(f"[gmail] service unavailable: {e}")
        return None

    # Gmail search query — `newer_than:` granularity is days, so we do
    # day-level here and filter to age in code.
    age_days = max(1, max_age_seconds // 86400 + 1)
    q = f'from:{sender_contains} newer_than:{age_days}d'
    try:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=10).execute()
    except Exception as e:
        logger.warning(f"[gmail] list failed: {e}")
        return None

    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    for short in (resp.get("messages") or []):
        try:
            full = svc.users().messages().get(
                userId="me", id=short["id"], format="full"
            ).execute()
        except Exception:
            continue
        ts = _msg_unix_ts(full)
        if ts and ts < cutoff:
            continue
        if body_must_contain:
            body = _get_text(full) or ""
            if body_must_contain.lower() not in body.lower():
                continue
        return full
    return None


def _extract_code(msg: dict) -> Optional[str]:
    subject = _header(msg, "Subject")
    body = _get_text(msg) or ""
    haystack = (subject or "") + "\n" + body
    for pat in _CODE_PATTERNS:
        m = pat.search(haystack)
        if m:
            return m.group(1)
    return None


def _get_text(msg: dict) -> Optional[str]:
    """Extract text/plain body (falls back to stripped text/html)."""
    payload = msg.get("payload") or {}
    text = _walk_parts(payload, "text/plain")
    if text:
        return text
    html = _walk_parts(payload, "text/html")
    if html:
        return re.sub(r"<[^>]+>", " ", html)
    return None


def _walk_parts(part: dict, mime: str) -> Optional[str]:
    if part.get("mimeType") == mime:
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                raw = base64.urlsafe_b64decode(data.encode("utf-8") + b"==")
                return raw.decode("utf-8", errors="replace")
            except Exception:
                pass
    for child in part.get("parts") or []:
        found = _walk_parts(child, mime)
        if found:
            return found
    return None


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload", {}).get("headers") or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _msg_unix_ts(msg: dict) -> Optional[float]:
    # Prefer Gmail's internalDate (millis since epoch)
    internal = msg.get("internalDate")
    if internal:
        try:
            return int(internal) / 1000.0
        except (TypeError, ValueError):
            pass
    date_hdr = _header(msg, "Date")
    if date_hdr:
        try:
            return parsedate_to_datetime(date_hdr).timestamp()
        except Exception:
            return None
    return None
