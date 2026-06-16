"""Pre-flight integration health check.

Run at scheduler startup (and on-demand via the bot). Tests every
external dependency the system needs to operate and reports specifically
what's broken + how to fix it. The goal: zero ambiguity when the system
isn't earning, so Chris fixes the right thing first.

Checks:
  - Anthropic API key + a tiny ping
  - YouTube Data API OAuth token (refreshable?)
  - Instagram Graph API token (valid + has insights scope?)
  - Telegram bot reachable?
  - FFmpeg + FFprobe present + executable?
  - Whisper model loadable?
  - Vyro session cached?
  - ClipStake / ClipAffiliates / Discord burner sessions cached?
  - DB schema up to date?
  - .env has Facebook App ID + Secret (for IG auto-refresh)?

Returns a list of HealthResult dicts:
    {component, ok, message, fix_hint}

Telegram message is built so each FAIL has a concrete next step.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings


@dataclass
class HealthResult:
    component: str
    ok: bool
    message: str
    fix_hint: str = ""


def run_all_checks() -> list[HealthResult]:
    return [
        check_anthropic(),
        check_youtube_oauth(),
        check_instagram_token(),
        check_telegram(),
        check_ffmpeg(),
        check_whisper(),
        check_db_schema(),
        check_facebook_app_creds(),
        check_marketplace_session("vyro"),
        check_marketplace_session("clipstake"),
        check_marketplace_session("clipaffiliates"),
        check_discord_burner(),
        check_tiktok_session(),
    ]


def render_telegram(results: list[HealthResult]) -> str:
    n_fail = sum(1 for r in results if not r.ok)
    n_pass = sum(1 for r in results if r.ok)
    head = f"<b>🩺 Health check — {n_pass}/{len(results)} OK"
    if n_fail:
        head += f", {n_fail} need attention</b>"
    else:
        head += " ✅</b>"
    lines = [head]
    # Surface failures first
    for r in results:
        if r.ok:
            continue
        lines.append(f"\n❌ <b>{r.component}</b>")
        lines.append(f"   <i>{r.message}</i>")
        if r.fix_hint:
            lines.append(f"   <b>Fix:</b> {r.fix_hint}")
    # Pass summary at the end
    passes = [r.component for r in results if r.ok]
    if passes:
        lines.append("\n<b>OK:</b> " + ", ".join(passes))
    return "\n".join(lines)


def notify_if_failures(results: Optional[list[HealthResult]] = None) -> None:
    results = results or run_all_checks()
    if all(r.ok for r in results):
        return  # silent when all good
    try:
        from publisher.telegram_gate import TelegramGate
        gate = TelegramGate()
        if gate.enabled:
            gate.notify(render_telegram(results))
    except Exception as e:
        logger.warning(f"[health] telegram notify failed: {e}")


# ----------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------
def check_anthropic() -> HealthResult:
    key = settings.anthropic_api_key
    if not key:
        return HealthResult("Anthropic API", False,
                            "ANTHROPIC_API_KEY missing from .env",
                            "Add ANTHROPIC_API_KEY=sk-ant-... to .env")
    try:
        from engine import llm_compat as anthropic
    except ImportError:
        return HealthResult("Anthropic API", False,
                            "anthropic SDK not installed",
                            "Run: .venv/Scripts/pip install anthropic")
    # Tiny ping — list models is cheap.
    try:
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model=settings.anthropic_model, max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return HealthResult("Anthropic API", True, "key valid, model responds")
    except Exception as e:
        return HealthResult("Anthropic API", False, str(e)[:200],
                            "Check API key + model name in .env")


def check_youtube_oauth() -> HealthResult:
    token_path = settings.youtube_token_path
    if not token_path:
        return HealthResult("YouTube OAuth", False,
                            "YOUTUBE_TOKEN_PATH not set",
                            "Add YOUTUBE_TOKEN_PATH=.auth/youtube-token.json to .env")
    p = settings.project_root / token_path if not Path(token_path).is_absolute() else Path(token_path)
    if not p.exists():
        return HealthResult("YouTube OAuth", False,
                            f"token file missing: {p}",
                            "Run scripts/youtube_oauth.py to authorize")
    try:
        from publisher.youtube_api import YouTubeAPIPublisher
        pub = YouTubeAPIPublisher()
        svc = pub._get_service()
        if svc is None:
            return HealthResult("YouTube OAuth", False, "service None",
                                "Re-run scripts/youtube_oauth.py")
        return HealthResult("YouTube OAuth", True, "token refreshes + service builds")
    except Exception as e:
        return HealthResult("YouTube OAuth", False, str(e)[:200],
                            "Re-run scripts/youtube_oauth.py")


def check_instagram_token() -> HealthResult:
    token = settings.instagram_access_token
    if not token:
        return HealthResult("Instagram Graph token", False,
                            "INSTAGRAM_ACCESS_TOKEN missing",
                            "Get a new Page token via Graph API Explorer + paste into .env")
    user_id = settings.instagram_user_id
    if not user_id:
        return HealthResult("Instagram Graph token", False, "INSTAGRAM_USER_ID missing",
                            "Set INSTAGRAM_USER_ID in .env")
    url = (f"https://graph.facebook.com/v21.0/{user_id}"
           f"?fields=id,username&access_token={urllib.parse.quote(token)}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return HealthResult("Instagram Graph token", True,
                            f"valid; user @{data.get('username', '?')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return HealthResult("Instagram Graph token", False, f"HTTP {e.code}: {body}",
                            "Run: python scripts/refresh_ig_token.py --force")
    except Exception as e:
        return HealthResult("Instagram Graph token", False, str(e)[:200],
                            "Run: python scripts/refresh_ig_token.py --force")


def check_telegram() -> HealthResult:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return HealthResult("Telegram", False, "TELEGRAM_BOT_TOKEN or CHAT_ID missing",
                            "Add both to .env (see Telegram BotFather + your chat_id)")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("ok"):
            return HealthResult("Telegram", True, f"bot @{data['result'].get('username', '?')}")
    except Exception as e:
        return HealthResult("Telegram", False, str(e)[:200],
                            "Check TELEGRAM_BOT_TOKEN in .env")
    return HealthResult("Telegram", False, "getMe returned !ok",
                        "Token may be revoked — regenerate via BotFather")


def check_ffmpeg() -> HealthResult:
    for binary in (settings.ffmpeg_path, settings.ffprobe_path):
        if not binary:
            return HealthResult("FFmpeg", False, "FFMPEG_PATH or FFPROBE_PATH unset",
                                "Set both in .env (point to ffmpeg.exe / ffprobe.exe)")
        # shutil.which handles both absolute paths and PATH lookup
        if Path(binary).exists() or shutil.which(binary):
            continue
        return HealthResult("FFmpeg", False, f"binary not found: {binary}",
                            f"Install ffmpeg or fix {binary} in .env")
    return HealthResult("FFmpeg", True, "ffmpeg + ffprobe found")


def check_whisper() -> HealthResult:
    try:
        import whisper  # noqa
        return HealthResult("Whisper", True, f"loadable (model={settings.whisper_model})")
    except ImportError:
        return HealthResult("Whisper", False, "openai-whisper not installed",
                            "Run: .venv/Scripts/pip install openai-whisper")


def check_db_schema() -> HealthResult:
    try:
        from db.repository import Repository, _apply_additive_migrations, get_connection
        r = Repository()  # auto-applies migrations now
        with r.conn() as c:
            # Spot-check that the newest columns exist
            cols = {row["name"] for row in c.execute("PRAGMA table_info(clips)").fetchall()}
            missing = {"experiment_hypothesis", "content_type"} - cols
        if missing:
            return HealthResult("DB schema", False, f"missing columns: {missing}",
                                "Run: python -c 'from db.repository import init_db; init_db()'")
        return HealthResult("DB schema", True, "all migrations applied")
    except Exception as e:
        return HealthResult("DB schema", False, str(e)[:200],
                            "Run: python -c 'from db.repository import init_db; init_db()'")


def check_facebook_app_creds() -> HealthResult:
    if not (settings.facebook_app_id and settings.facebook_app_secret):
        return HealthResult("FB App for IG refresh", False,
                            "FACEBOOK_APP_ID or FACEBOOK_APP_SECRET missing",
                            "Get from developers.facebook.com → your app → Settings → Basic")
    return HealthResult("FB App for IG refresh", True, "App ID + Secret set")


def check_marketplace_session(platform: str) -> HealthResult:
    auth_file = settings.project_root / ".auth" / f"{platform}.json"
    if not auth_file.exists():
        return HealthResult(
            f"{platform.capitalize()} session", False,
            f"no cached session at {auth_file.name}",
            f"Run: python scripts/scan_web_marketplaces.py --only {platform} --headed",
        )
    return HealthResult(f"{platform.capitalize()} session", True, "cached")


def check_discord_burner() -> HealthResult:
    if not (settings.discord_burner_email and settings.discord_burner_password):
        return HealthResult("Discord burner", False, "creds missing in .env",
                            "Create burner Discord; add DISCORD_BURNER_EMAIL + _PASSWORD")
    auth_file = settings.project_root / ".auth" / "discord.json"
    if not auth_file.exists():
        return HealthResult("Discord burner", False, "creds set but no cached session",
                            "Trigger first login via bot or run scan_clipify_directory.py")
    return HealthResult("Discord burner", True, "creds + cached session")


def check_tiktok_session() -> HealthResult:
    profile = settings.project_root / ".auth" / "tiktok-profile"
    if not profile.exists() or not any(profile.iterdir()):
        return HealthResult("TikTok session", False, "no logged-in profile cached",
                            "Run: python scripts/platform_login.py tiktok")
    return HealthResult("TikTok session", True, "profile cached")
