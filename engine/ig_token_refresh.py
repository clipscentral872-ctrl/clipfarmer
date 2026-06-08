"""Auto-refresh the Instagram Graph API access token.

The Facebook user-token from Graph API Explorer is short-lived (~1 hour).
We exchange it for a 60-day long-lived user token via the documented
`fb_exchange_token` flow, then re-derive a Page token (which inherits
the long-lived property and effectively never expires while the password
holds).

Updates `.env` in place with the new token, so the publisher's next
upload uses it immediately.

Public surface:
    from engine.ig_token_refresh import refresh
    new_token, expires_in = refresh()

Or via the CLI: `python scripts/refresh_ig_token.py`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


API_BASE = "https://graph.facebook.com/v21.0"
ENV_PATH = settings.project_root / ".env"
SAFETY_DAYS_BEFORE_EXPIRY = 7  # refresh if token expires within this many days


class IGTokenError(RuntimeError):
    pass


def refresh(force: bool = False) -> tuple[str, Optional[int]]:
    """Exchange the current token for a long-lived Page token + write to .env.

    Returns (new_token, expires_in_seconds_or_None).
    Raises IGTokenError if required creds aren't set.
    """
    app_id = settings.facebook_app_id
    app_secret = settings.facebook_app_secret
    cur_token = settings.instagram_access_token
    if not (app_id and app_secret and cur_token):
        raise IGTokenError(
            "Need FACEBOOK_APP_ID, FACEBOOK_APP_SECRET, and a valid current "
            "INSTAGRAM_ACCESS_TOKEN in .env"
        )

    if not force:
        debug = _debug_token(cur_token, app_id, app_secret)
        if debug and debug.get("expires_in_days") and debug["expires_in_days"] > SAFETY_DAYS_BEFORE_EXPIRY:
            logger.info(
                f"[ig-refresh] current token still valid for "
                f"{debug['expires_in_days']:.1f} days; skipping refresh"
            )
            return cur_token, debug.get("expires_at_unix")

    # Step 1: exchange short-lived → long-lived USER token (60 days).
    long_user = _exchange_for_long_lived_user(cur_token, app_id, app_secret)
    # Step 2: derive Page tokens. Page tokens from a long-lived user token
    # do not expire as long as the password / permissions hold.
    pages = _list_pages(long_user["access_token"])
    if not pages:
        raise IGTokenError("No Pages found on this account; can't derive Page token")
    # Pick the Page that owns the IG business account (or just the first).
    target_page_id = _pick_target_page_id(pages)
    page_token = next(p["access_token"] for p in pages if p["id"] == target_page_id)

    _write_to_env(page_token)
    logger.info(f"[ig-refresh] wrote new long-lived Page token to {ENV_PATH}")
    return page_token, long_user.get("expires_in")


def _exchange_for_long_lived_user(short_token: str, app_id: str, app_secret: str) -> dict:
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }
    url = f"{API_BASE}/oauth/access_token?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url)
    if not data or "error" in data:
        raise IGTokenError(f"long-lived exchange failed: {data}")
    return data


def _list_pages(user_token: str) -> list[dict]:
    url = f"{API_BASE}/me/accounts?access_token={urllib.parse.quote(user_token)}"
    data = _http_get_json(url)
    if not data or "error" in data:
        raise IGTokenError(f"/me/accounts failed: {data}")
    return data.get("data") or []


def _pick_target_page_id(pages: list[dict]) -> str:
    """Prefer the Page connected to the IG business account in settings; else first."""
    ig_user_id = settings.instagram_user_id
    if ig_user_id:
        # Look for a Page whose instagram_business_account matches.
        for p in pages:
            pid = p["id"]
            ig_link = _http_get_json(
                f"{API_BASE}/{pid}?fields=instagram_business_account&access_token={urllib.parse.quote(p['access_token'])}"
            )
            if ig_link and (ig_link.get("instagram_business_account") or {}).get("id") == ig_user_id:
                return pid
    return pages[0]["id"]


def _debug_token(token: str, app_id: str, app_secret: str) -> Optional[dict]:
    """Use /debug_token to check expiry. Returns {expires_in_days, expires_at_unix}
    or None if the call fails."""
    app_token = f"{app_id}|{app_secret}"
    url = (
        f"{API_BASE}/debug_token"
        f"?input_token={urllib.parse.quote(token)}"
        f"&access_token={urllib.parse.quote(app_token)}"
    )
    data = _http_get_json(url)
    if not data or "data" not in data:
        return None
    d = data["data"]
    exp_unix = d.get("expires_at") or 0
    if exp_unix == 0:
        # 0 = never expires
        return {"expires_in_days": float("inf"), "expires_at_unix": 0}
    now = datetime.now(timezone.utc).timestamp()
    return {
        "expires_in_days": (exp_unix - now) / 86400.0,
        "expires_at_unix": exp_unix,
    }


def _write_to_env(new_token: str) -> None:
    """Replace the INSTAGRAM_ACCESS_TOKEN line in .env in place."""
    if not ENV_PATH.exists():
        raise IGTokenError(f".env not found at {ENV_PATH}")
    content = ENV_PATH.read_text(encoding="utf-8")
    pat = re.compile(r"^INSTAGRAM_ACCESS_TOKEN\s*=.*$", re.MULTILINE)
    if pat.search(content):
        content = pat.sub(f"INSTAGRAM_ACCESS_TOKEN={new_token}", content)
    else:
        content = content.rstrip() + f"\nINSTAGRAM_ACCESS_TOKEN={new_token}\n"
    ENV_PATH.write_text(content, encoding="utf-8")
    # Re-hydrate the live settings so the running process picks it up.
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = new_token


def _http_get_json(url: str, timeout: int = 20) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "clipfarmer-ig-refresh/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return None
    except Exception as e:
        logger.warning(f"[ig-refresh] http error: {e}")
        return None
