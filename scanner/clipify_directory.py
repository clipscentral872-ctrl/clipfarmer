"""Discover new Clipify per-streamer campaigns automatically.

Polls two sources from the burner DiscordSession's perspective:

  1. **Sx Bot Clipify main server → #active-campaigns** — Clipify's central
     directory. Each entry is a streamer's payout/rules summary with a
     join-this-server invite link. We capture the invite + display info.

  2. **Each joined per-streamer Clipify server → #campaign-details** —
     once joined, this channel has the full brief (CPM, hashtags, mentions,
     forbidden content). We feed that text into the existing
     `engine.rules_extractor` so the campaign gets structured rules.

Output: rows in the `campaigns` table with marketplace='clipify',
marketplace_server set to the streamer's server name, and structured_rules
populated. Same shape the existing pipeline already consumes.

This module RUNS against a logged-in DiscordSession. Without burner creds
it will refuse to start.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config import settings
from db.repository import Repository
from scanner.discord_session import DiscordSession


SX_BOT_CLIPIFY_SERVER_NAME = "Sx Bot Clipify"
ACTIVE_CAMPAIGNS_CHANNEL = "active-campaigns"
CAMPAIGN_DETAILS_CHANNEL = "campaign-details"

DISCORD_INVITE_RE = re.compile(
    r"https?://(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9_-]+", re.IGNORECASE
)


@dataclass
class DirectoryEntry:
    streamer_name: str
    server_invite: Optional[str]
    teaser_text: str  # the raw active-campaigns message text


class ClipifyDirectoryScanner:
    """Pulls listings from #active-campaigns and per-server #campaign-details."""

    def __init__(self, session: Optional[DiscordSession] = None) -> None:
        if not (settings.discord_burner_email and settings.discord_burner_password):
            raise RuntimeError(
                "Burner Discord creds missing — set DISCORD_BURNER_EMAIL "
                "and DISCORD_BURNER_PASSWORD in .env"
            )
        self._owns_session = session is None
        self._session = session

    def __enter__(self) -> "ClipifyDirectoryScanner":
        if self._session is None:
            self._session = DiscordSession()
            self._session.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    @property
    def session(self) -> DiscordSession:
        if self._session is None:
            raise RuntimeError("ClipifyDirectoryScanner not started")
        return self._session

    # ------------------------------------------------------------------
    # Directory scan
    # ------------------------------------------------------------------
    def scan_directory(self) -> list[DirectoryEntry]:
        """Scrape the Sx Bot Clipify main server's #active-campaigns channel."""
        self.session.open_server(SX_BOT_CLIPIFY_SERVER_NAME)
        self.session.open_channel(ACTIVE_CAMPAIGNS_CHANNEL)
        time.sleep(1.0)
        page = self.session.page

        # Scroll up a few times to catch the older entries.
        for _ in range(4):
            page.mouse.wheel(0, -3000)
            time.sleep(0.6)

        messages_text = page.evaluate(_COLLECT_MESSAGES_JS)
        entries: list[DirectoryEntry] = []
        seen_invites: set[str] = set()
        for msg in messages_text:
            text = (msg or "").strip()
            if not text:
                continue
            invites = DISCORD_INVITE_RE.findall(text)
            invite = invites[0] if invites else None
            if invite and invite in seen_invites:
                continue
            if invite:
                seen_invites.add(invite)
            streamer = _guess_streamer_name(text)
            if streamer:
                entries.append(
                    DirectoryEntry(
                        streamer_name=streamer,
                        server_invite=invite,
                        teaser_text=text[:1500],
                    )
                )
        logger.info(f"[directory] {len(entries)} entries in #active-campaigns")
        return entries

    # ------------------------------------------------------------------
    # Per-server detail scrape
    # ------------------------------------------------------------------
    def scrape_campaign_details(self, server_name: str) -> Optional[str]:
        """Open the streamer's server and grab the #campaign-details text."""
        try:
            self.session.open_server(server_name)
            self.session.open_channel(CAMPAIGN_DETAILS_CHANNEL)
        except RuntimeError as e:
            logger.warning(f"[directory] couldn't open {server_name}: {e}")
            return None
        time.sleep(1.0)
        # Pin the top of the channel (campaign-details is typically pinned/static)
        try:
            self.session.page.mouse.wheel(0, -8000)
            time.sleep(0.6)
        except Exception:
            pass
        messages = self.session.page.evaluate(_COLLECT_MESSAGES_JS)
        full = "\n\n".join(m for m in messages if m)
        return full or None

    # ------------------------------------------------------------------
    # Ingest discovered campaigns into the DB
    # ------------------------------------------------------------------
    def ingest_into_db(self, repo: Repository, entries: list[DirectoryEntry]) -> dict:
        """For each entry: if the burner is already in that server, scrape
        #campaign-details, extract rules, upsert the campaign row.

        Entries the burner hasn't joined yet are logged + returned so the
        caller can Telegram-prompt Chris to join from the burner.
        """
        from engine.rules_extractor import extract_rules, RulesExtractionError

        joined_servers = self._list_joined_servers()

        added: list[str] = []
        updated: list[str] = []
        not_joined: list[DirectoryEntry] = []

        for entry in entries:
            server_candidates = [
                f"{entry.streamer_name} X Clipify",
                f"{entry.streamer_name} x Clipify",
                entry.streamer_name,
            ]
            matched_server = next(
                (s for s in joined_servers if any(c.lower() == s.lower() for c in server_candidates)),
                None,
            )
            if not matched_server:
                # try fuzzy "starts with"
                matched_server = next(
                    (s for s in joined_servers
                     if s.lower().startswith(entry.streamer_name.lower())),
                    None,
                )
            if not matched_server:
                not_joined.append(entry)
                continue

            brief = self.scrape_campaign_details(matched_server)
            if not brief or len(brief) < 80:
                logger.warning(f"[directory] {matched_server}: no brief found")
                continue

            try:
                rules = extract_rules(brief, campaign_title=matched_server)
            except RulesExtractionError as e:
                logger.warning(f"[directory] {matched_server}: rules extract failed ({e})")
                continue

            whop_id = f"clipify::{matched_server}"
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with repo.conn() as c:
                existing = c.execute(
                    "SELECT id FROM campaigns WHERE whop_campaign_id = ?", (whop_id,),
                ).fetchone()
                if existing:
                    c.execute(
                        "UPDATE campaigns SET title=?, marketplace=?, marketplace_server=?, "
                        "campaign_brief=?, structured_rules=?, last_seen_at=? WHERE id=?",
                        (matched_server, "clipify", matched_server,
                         brief, json.dumps(rules), now, existing["id"]),
                    )
                    updated.append(matched_server)
                else:
                    c.execute(
                        "INSERT INTO campaigns ("
                        "whop_campaign_id, community_id, community_name, title, "
                        "marketplace, marketplace_server, campaign_brief, structured_rules, "
                        "status, discovered_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (whop_id, "clipify", "Clipify", matched_server,
                         "clipify", matched_server, brief, json.dumps(rules),
                         "active", now, now),
                    )
                    added.append(matched_server)

        return {"added": added, "updated": updated, "not_joined": not_joined}

    # ------------------------------------------------------------------
    def _list_joined_servers(self) -> list[str]:
        """Return aria-label strings of every server icon in the burner's sidebar."""
        nav = self.session.page.locator('[aria-label="Servers"]')
        try:
            nav.wait_for(state="visible", timeout=10_000)
        except Exception:
            return []
        return self.session.page.evaluate(
            """(() => {
                const nav = document.querySelector('[aria-label="Servers"]');
                if (!nav) return [];
                return Array.from(nav.querySelectorAll('[aria-label]'))
                    .map(el => el.getAttribute('aria-label'))
                    .filter(Boolean);
            })()"""
        )


# ----------------------------------------------------------------------
# Helpers (pure)
# ----------------------------------------------------------------------
def _guess_streamer_name(text: str) -> Optional[str]:
    """Pull a streamer name from a #active-campaigns message.

    Clipify entries tend to lead with "<Streamer> X Clipify" or
    "**<Streamer>**" on the first non-empty line.
    """
    for line in text.splitlines():
        line = line.strip().lstrip("*_~`#").strip()
        if not line:
            continue
        # Strip the trailing "X Clipify" / "x Clipify" suffix if present.
        m = re.match(r"^(.+?)\s+[xX]\s+Clipify\b", line)
        if m:
            return m.group(1).strip()
        # First short line that's not a URL is a reasonable guess.
        if "http" in line.lower():
            continue
        if 2 <= len(line) <= 60:
            return line.split("·")[0].strip()
    return None


_COLLECT_MESSAGES_JS = r"""
(() => {
    const out = [];
    document.querySelectorAll('[id^="chat-messages-"]').forEach(el => {
        const txt = (el.innerText || "").trim();
        if (txt) out.push(txt);
    });
    return out;
})()
"""
