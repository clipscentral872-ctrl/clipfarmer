"""Central settings loader.

Reads from environment variables (and a local .env file if present).
Import the singleton `settings` and use dot-access:

    from config import settings
    settings.anthropic_api_key
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
CLIPS_DIR = DATA_DIR / "clips"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
LOGS_DIR = PROJECT_ROOT / "logs"


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(key, default)
    return val if val not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class Settings:
    # --- Paths -------------------------------------------------------------
    project_root: Path = PROJECT_ROOT
    download_dir: Path = DOWNLOAD_DIR
    clips_dir: Path = CLIPS_DIR
    screenshots_dir: Path = SCREENSHOTS_DIR
    logs_dir: Path = LOGS_DIR

    # --- Whop --------------------------------------------------------------
    whop_email: Optional[str] = field(default_factory=lambda: _env("WHOP_EMAIL"))
    whop_password: Optional[str] = field(default_factory=lambda: _env("WHOP_PASSWORD"))
    whop_communities: list[str] = field(
        default_factory=lambda: [
            c.strip() for c in (_env("WHOP_COMMUNITIES", "") or "").split(",") if c.strip()
        ]
    )

    # --- Burner Gmail (for fetching verification codes automatically) ----
    # Uses Gmail API + OAuth (App Passwords no longer issued on newer accounts).
    # Same shape as YouTube OAuth: download client_secret.json from Google
    # Cloud Console (Gmail API + OAuth Desktop client), point GMAIL_CLIENT_SECRET_PATH
    # at it; the token cache auto-creates and refreshes forever.
    gmail_client_secret_path: Optional[str] = field(default_factory=lambda: _env("GMAIL_CLIENT_SECRET_PATH"))
    gmail_token_path: Optional[str] = field(default_factory=lambda: _env("GMAIL_TOKEN_PATH"))

    # --- Discord (burner account for Clipify /clips add submissions) ------
    # Use a SEPARATE Discord account, not your main. The submitter pretends
    # to be a user in the Discord web client to fire slash commands at
    # Clipify, which is a ToS violation. Worst case: burner is banned.
    discord_burner_email: Optional[str] = field(default_factory=lambda: _env("DISCORD_BURNER_EMAIL"))
    discord_burner_password: Optional[str] = field(default_factory=lambda: _env("DISCORD_BURNER_PASSWORD"))

    # --- Vyro (web marketplace; tracks posts via connected socials) -------
    vyro_email: Optional[str] = field(default_factory=lambda: _env("VYRO_EMAIL"))
    vyro_password: Optional[str] = field(default_factory=lambda: _env("VYRO_PASSWORD"))

    # --- ClipStake (web marketplace) --------------------------------------
    clipstake_email: Optional[str] = field(default_factory=lambda: _env("CLIPSTAKE_EMAIL"))
    clipstake_password: Optional[str] = field(default_factory=lambda: _env("CLIPSTAKE_PASSWORD"))

    # --- ClipAffiliates ---------------------------------------------------
    clipaffiliates_email: Optional[str] = field(default_factory=lambda: _env("CLIPAFFILIATES_EMAIL"))
    clipaffiliates_password: Optional[str] = field(default_factory=lambda: _env("CLIPAFFILIATES_PASSWORD"))

    # --- Anthropic ---------------------------------------------------------
    anthropic_api_key: Optional[str] = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"))

    # --- Whisper -----------------------------------------------------------
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))

    # --- Metricool (legacy, kept for backwards compat — unused) -----------
    metricool_api_token: Optional[str] = field(default_factory=lambda: _env("METRICOOL_API_TOKEN"))
    metricool_brand_id: Optional[str] = field(default_factory=lambda: _env("METRICOOL_BRAND_ID"))
    metricool_user_id: Optional[str] = field(default_factory=lambda: _env("METRICOOL_USER_ID"))
    metricool_tiktok_account_id: Optional[str] = field(default_factory=lambda: _env("METRICOOL_TIKTOK_ACCOUNT_ID"))
    metricool_youtube_account_id: Optional[str] = field(default_factory=lambda: _env("METRICOOL_YOUTUBE_ACCOUNT_ID"))
    metricool_instagram_account_id: Optional[str] = field(default_factory=lambda: _env("METRICOOL_INSTAGRAM_ACCOUNT_ID"))

    # --- YouTube Data API v3 (official, free) -----------------------------
    youtube_client_secret_path: Optional[str] = field(default_factory=lambda: _env("YOUTUBE_CLIENT_SECRET_PATH"))
    youtube_token_path: Optional[str] = field(default_factory=lambda: _env("YOUTUBE_TOKEN_PATH"))

    # --- Instagram Graph API (official, free) -----------------------------
    instagram_user_id: Optional[str] = field(default_factory=lambda: _env("INSTAGRAM_USER_ID"))
    instagram_access_token: Optional[str] = field(default_factory=lambda: _env("INSTAGRAM_ACCESS_TOKEN"))
    # Facebook App credentials — used by the nightly IG token auto-refresh
    # so we never hit "Session has expired" again. App ID is public; secret
    # must be in .env only (never committed). Refresh exchanges the current
    # token for a 60-day long-lived one, then re-derives the Page token.
    facebook_app_id: Optional[str] = field(default_factory=lambda: _env("FACEBOOK_APP_ID"))
    facebook_app_secret: Optional[str] = field(default_factory=lambda: _env("FACEBOOK_APP_SECRET"))

    # --- Telegram (approval + notifications) ------------------------------
    telegram_bot_token: Optional[str] = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: Optional[str] = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    require_telegram_approval: bool = field(default_factory=lambda: _env_bool("REQUIRE_TELEGRAM_APPROVAL", True))

    # Quiet hours: pings outside this window get queued + flushed at the
    # next active-window start. Defaults match Chris's SAST afternoon window.
    # Anything urgent (`urgent=True` in notify/send_photo) bypasses the gate.
    quiet_hours_enabled: bool = field(default_factory=lambda: _env_bool("QUIET_HOURS_ENABLED", True))
    quiet_hours_tz: str = field(default_factory=lambda: _env("QUIET_HOURS_TZ", "Africa/Johannesburg"))
    quiet_hours_window_start: str = field(default_factory=lambda: _env("QUIET_HOURS_WINDOW_START", "15:00"))
    quiet_hours_window_end: str = field(default_factory=lambda: _env("QUIET_HOURS_WINDOW_END", "21:30"))

    # --- Scheduling --------------------------------------------------------
    scan_interval_minutes: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_MINUTES", 60))
    post_interval_minutes: int = field(default_factory=lambda: _env_int("POST_INTERVAL_MINUTES", 90))
    submission_screenshot_after_hours: int = field(default_factory=lambda: _env_int("SCREENSHOT_AFTER_HOURS", 48))

    # --- Clip engine -------------------------------------------------------
    clip_min_seconds: int = field(default_factory=lambda: _env_int("CLIP_MIN_SECONDS", 30))
    clip_max_seconds: int = field(default_factory=lambda: _env_int("CLIP_MAX_SECONDS", 60))
    clips_per_source: int = field(default_factory=lambda: _env_int("CLIPS_PER_SOURCE", 3))

    # --- Vision scoring ----------------------------------------------------
    enable_vision_scoring: bool = field(default_factory=lambda: _env_bool("ENABLE_VISION_SCORING", True))
    vision_text_weight: float = field(default_factory=lambda: _env_float("VISION_TEXT_WEIGHT", 0.55))
    vision_visual_weight: float = field(default_factory=lambda: _env_float("VISION_VISUAL_WEIGHT", 0.45))

    # --- Vision-guided crop ------------------------------------------------
    enable_vision_crop: bool = field(default_factory=lambda: _env_bool("ENABLE_VISION_CROP", True))
    # 2.4 keeps generous context around the subject (food, props, hands), not
    # just a tight head + shoulders crop. Useful for food / demo / action content
    # where what the presenter is showing is half the point.
    vision_crop_margin: float = field(default_factory=lambda: _env_float("VISION_CROP_MARGIN", 2.4))

    # --- Campaign viability filter ----------------------------------------
    min_budget_remaining_pct: float = field(default_factory=lambda: _env_float("MIN_BUDGET_REMAINING_PCT", 60.0))

    # --- FFmpeg ------------------------------------------------------------
    ffmpeg_path: str = field(default_factory=lambda: _env("FFMPEG_PATH", "ffmpeg"))
    ffprobe_path: str = field(default_factory=lambda: _env("FFPROBE_PATH", "ffprobe"))

    def ensure_dirs(self) -> None:
        for d in (self.download_dir, self.clips_dir, self.screenshots_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()


def _ensure_ffmpeg_on_path() -> None:
    """Whisper, ffmpeg-python, and other libs shell out to ffmpeg via PATH
    only. If FFMPEG_PATH is an absolute path, add its directory to PATH so
    those callers find it."""
    fp = Path(settings.ffmpeg_path)
    if fp.is_absolute() and fp.exists():
        ffmpeg_dir = str(fp.parent)
        sep = os.pathsep
        current = os.environ.get("PATH", "")
        if ffmpeg_dir not in current.split(sep):
            os.environ["PATH"] = f"{ffmpeg_dir}{sep}{current}"


_ensure_ffmpeg_on_path()
