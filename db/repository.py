"""SQLite access layer for clipfarmer.

Thin wrapper around sqlite3 with row_factory set to sqlite3.Row so callers
get dict-like access. All write helpers return the new row's id.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "clipfarmer.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH, schema_path: Path = SCHEMA_PATH) -> None:
    """Create tables if they don't exist, and apply additive column migrations."""
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(db_path) as conn:
        conn.executescript(sql)
        _apply_additive_migrations(conn)


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for fields added after the original schema."""
    additions = [
        ("campaigns", "top_performers", "TEXT"),
        ("campaigns", "campaign_brief", "TEXT"),
        ("campaigns", "structured_rules", "TEXT"),
        ("campaigns", "source_links", "TEXT"),
        ("campaigns", "current_source_path", "TEXT"),
        ("campaigns", "tracking_code", "TEXT"),
        ("campaigns", "format_mode_default", "TEXT"),
        # Multi-marketplace support: 'whop' for the existing Whop community,
        # 'clipify' for Sx Bot Clipify per-streamer servers, plus room for
        # 'clipstake', 'vyro', 'clipaffiliates', etc. The submission router
        # uses this to pick the right outgoing flow (web-form for whop,
        # Discord paste-command for clipify, etc.).
        ("campaigns", "marketplace", "TEXT"),
        # Discord-specific: which server's #commands or #general channel
        # the user pastes /clips add into. Free-form because we may store
        # the server name only or an invite URL depending on the source.
        ("campaigns", "marketplace_server", "TEXT"),
        ("posts", "analytics_48hr_notified_at", "TEXT"),
        # Content style (person-to-camera / reaction / demo / conversation /
        # narration / montage / other). Set by engine.style_classifier at
        # production time; used by the Brain to learn which styles win per
        # campaign and by the exploit/explore allocator.
        ("clips", "content_type", "TEXT"),
        ("clips", "content_type_reason", "TEXT"),
        # Brain experiment ID + hypothesis text. Set when a clip is produced
        # under a queued auto-experiment so outcomes can be attributed back
        # to the specific bet being tested.
        ("clips", "experiment_hypothesis", "TEXT"),
        ("clips", "experiment_params", "TEXT"),
        # Content warehouse (3-day buffer per Chris's 2026-06-08 spec).
        # Clips are produced N days ahead of their scheduled post slot,
        # refined for ~48h, then surfaced to Chris for review 24h before
        # going live. See scripts/produce_warehouse.py + refine_warehouse.py
        # + promote_for_review.py for the lifecycle.
        ("clips", "warehouse_state", "TEXT"),       # warehouse | refining | pending_review | approved | rejected | posted | discarded
        ("clips", "scheduled_post_at", "TEXT"),     # ISO-8601 UTC; when this clip is slated to publish
        ("clips", "refinement_count", "INTEGER DEFAULT 0"),
        ("clips", "last_refined_at", "TEXT"),
        ("clips", "review_sent_at", "TEXT"),        # when promoted to Chris's Telegram
        ("clips", "review_token", "TEXT"),          # Telegram approval message token
        ("clips", "reviewed_at", "TEXT"),
        ("clips", "review_verdict", "TEXT"),        # approved | rejected
        ("clips", "review_note", "TEXT"),           # Chris's rejection reason if any
    ]
    for table, col, coltype in additions:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


_MIGRATIONS_APPLIED = False


class Repository:
    """High-level CRUD helpers used by every module."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        # Auto-apply additive migrations on first Repository construction in
        # this process. Without this, freshly-added columns crash callers
        # until someone manually runs init_db().
        global _MIGRATIONS_APPLIED
        if not _MIGRATIONS_APPLIED:
            try:
                with get_connection(db_path) as conn:
                    _apply_additive_migrations(conn)
                _MIGRATIONS_APPLIED = True
            except Exception:
                # Don't crash on read-only environments; init_db() can be
                # called explicitly elsewhere if migrations are needed.
                pass

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = get_connection(self.db_path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ----- campaigns -----------------------------------------------------
    def upsert_campaign(self, data: dict[str, Any]) -> int:
        cols = (
            "whop_campaign_id", "community_id", "community_name", "title",
            "description", "payout_per_1k_views", "payout_currency",
            "min_duration_sec", "max_duration_sec", "platforms_required",
            "rules", "submission_url", "status",
            "budget_total", "budget_remaining", "budget_remaining_pct",
            "min_payout_threshold", "min_views_for_payout",
            "approval_rate", "campaign_frequency", "viability_score",
            "ends_at",
        )
        # Default status to 'active' if scanner didn't set one — the scheduler's
        # picker filters by status and a NULL would silently exclude the row.
        data.setdefault("status", "active")
        values = [data.get(c) for c in cols]
        if isinstance(data.get("platforms_required"), list):
            idx = cols.index("platforms_required")
            values[idx] = json.dumps(data["platforms_required"])

        with self.conn() as c:
            existing = c.execute(
                "SELECT id FROM campaigns WHERE whop_campaign_id = ?",
                (data["whop_campaign_id"],),
            ).fetchone()
            now = _now()
            if existing:
                c.execute(
                    f"UPDATE campaigns SET "
                    + ", ".join(f"{col}=?" for col in cols)
                    + ", last_seen_at=? WHERE id=?",
                    (*values, now, existing["id"]),
                )
                return existing["id"]
            c.execute(
                f"INSERT INTO campaigns ({', '.join(cols)}, discovered_at, last_seen_at) "
                f"VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                (*values, now, now),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def list_active_campaigns(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM campaigns WHERE status = 'active' ORDER BY discovered_at DESC"
            ).fetchall()

    def set_campaign_tracking_code(self, campaign_id: int, code: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE campaigns SET tracking_code=?, last_seen_at=? WHERE id=?",
                (code, _now(), campaign_id),
            )

    def set_campaign_current_source(self, campaign_id: int, source_path: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE campaigns SET current_source_path=?, last_seen_at=? WHERE id=?",
                (source_path, _now(), campaign_id),
            )

    def set_campaign_source_links(self, campaign_id: int, links: list[str]) -> None:
        payload = json.dumps(links) if links else None
        with self.conn() as c:
            c.execute(
                "UPDATE campaigns SET source_links=?, last_seen_at=? WHERE id=?",
                (payload, _now(), campaign_id),
            )

    def set_campaign_brief(self, campaign_id: int, brief_text: str, structured_rules: Optional[dict[str, Any]] = None) -> None:
        """Store the raw campaign brief and (optionally) its extracted structured rules."""
        rules_payload = json.dumps(structured_rules) if structured_rules else None
        with self.conn() as c:
            c.execute(
                "UPDATE campaigns SET campaign_brief=?, structured_rules=?, last_seen_at=? WHERE id=?",
                (brief_text, rules_payload, _now(), campaign_id),
            )

    def get_campaign_structured_rules(self, campaign_id: int) -> Optional[dict[str, Any]]:
        with self.conn() as c:
            row = c.execute(
                "SELECT structured_rules FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        if not row or not row["structured_rules"]:
            return None
        try:
            return json.loads(row["structured_rules"])
        except json.JSONDecodeError:
            return None

    def set_campaign_top_performers(self, campaign_id: int, performers: list[dict[str, Any]]) -> None:
        """Replace the top_performers JSON blob for a campaign.

        Kept separate from upsert_campaign so the scanner doesn't clobber
        scraped or manually-seeded top-performer data on subsequent refreshes.
        """
        payload = json.dumps(performers) if performers else None
        with self.conn() as c:
            c.execute(
                "UPDATE campaigns SET top_performers=?, last_seen_at=? WHERE id=?",
                (payload, _now(), campaign_id),
            )

    # ----- source_videos -------------------------------------------------
    def add_source_video(self, campaign_id: int, source_url: str, title: Optional[str] = None) -> int:
        with self.conn() as c:
            existing = c.execute(
                "SELECT id FROM source_videos WHERE campaign_id=? AND source_url=?",
                (campaign_id, source_url),
            ).fetchone()
            if existing:
                return existing["id"]
            c.execute(
                "INSERT INTO source_videos (campaign_id, source_url, title, discovered_at) "
                "VALUES (?, ?, ?, ?)",
                (campaign_id, source_url, title, _now()),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def set_source_video_field(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE source_videos SET {cols} WHERE id=?", (*fields.values(), source_id))

    def list_source_videos_by_status(self, field: str, value: str) -> list[sqlite3.Row]:
        assert field in {"download_status", "transcribe_status", "score_status"}
        with self.conn() as c:
            return c.execute(
                f"SELECT * FROM source_videos WHERE {field} = ?", (value,)
            ).fetchall()

    # ----- clips ---------------------------------------------------------
    def add_clip(self, data: dict[str, Any]) -> int:
        cols = (
            "source_video_id", "campaign_id", "start_sec", "end_sec",
            "duration_sec", "transcript_excerpt", "ai_score", "ai_reason",
            "hook_text", "caption_text", "suggested_hashtags",
            "content_type", "content_type_reason",
            "experiment_hypothesis", "experiment_params",
        )
        values = [data.get(c) for c in cols]
        if isinstance(data.get("suggested_hashtags"), list):
            idx = cols.index("suggested_hashtags")
            values[idx] = json.dumps(data["suggested_hashtags"])
        with self.conn() as c:
            c.execute(
                f"INSERT INTO clips ({', '.join(cols)}, created_at) "
                f"VALUES ({', '.join('?' * len(cols))}, ?)",
                (*values, _now()),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def set_clip_field(self, clip_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE clips SET {cols} WHERE id=?", (*fields.values(), clip_id))

    def list_clips_by_status(self, status: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM clips WHERE status = ? ORDER BY ai_score DESC", (status,)
            ).fetchall()

    # ----- content warehouse (3-day buffer) ------------------------------
    def warehouse_counts_per_day(self, days_ahead: int = 3) -> dict[int, int]:
        """Return {day_offset: clip_count} for clips currently in the
        warehouse (warehouse | refining | pending_review | approved). Used by
        the producer to decide whether D+1 / D+2 / D+3 need topping up."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        counts: dict[int, int] = {d: 0 for d in range(1, days_ahead + 1)}
        with self.conn() as c:
            rows = c.execute(
                "SELECT scheduled_post_at FROM clips "
                "WHERE warehouse_state IN ('warehouse','refining','pending_review','approved') "
                "AND scheduled_post_at IS NOT NULL"
            ).fetchall()
        for r in rows:
            try:
                t = datetime.fromisoformat(r["scheduled_post_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            offset = (t.date() - now.date()).days
            if 1 <= offset <= days_ahead:
                counts[offset] = counts.get(offset, 0) + 1
        return counts

    def warehouse_clips_for_refinement(self, max_passes: int = 3, min_hours_until_post: int = 24) -> list[sqlite3.Row]:
        """Clips that should get another QA/Editor pass: still in warehouse,
        scheduled to post more than `min_hours_until_post` ahead (so we don't
        refine ones we're about to ship), and haven't hit the per-clip
        refinement cap."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=min_hours_until_post)).isoformat()
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM clips "
                "WHERE warehouse_state = 'warehouse' "
                "AND (refinement_count IS NULL OR refinement_count < ?) "
                "AND scheduled_post_at > ? "
                "ORDER BY ai_score ASC, refinement_count ASC",   # weakest first
                (max_passes, cutoff),
            ).fetchall()

    def warehouse_clips_due_for_review(self, within_hours: int = 24) -> list[sqlite3.Row]:
        """Clips scheduled to post within the next N hours that haven't been
        promoted to Chris's Telegram yet."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=within_hours)).isoformat()
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM clips "
                "WHERE warehouse_state IN ('warehouse','refining') "
                "AND scheduled_post_at IS NOT NULL "
                "AND scheduled_post_at <= ? "
                "ORDER BY scheduled_post_at ASC",
                (cutoff,),
            ).fetchall()

    def warehouse_approved_clip_for_slot(self, slot_start_iso: str, slot_end_iso: str) -> Optional[sqlite3.Row]:
        """Find one approved clip slated to post within this slot's window.
        Returns None if the warehouse is empty for the slot — slot script
        then falls back to producing fresh."""
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM clips "
                "WHERE warehouse_state = 'approved' "
                "AND scheduled_post_at BETWEEN ? AND ? "
                "ORDER BY ai_score DESC LIMIT 1",
                (slot_start_iso, slot_end_iso),
            ).fetchone()

    def mark_clip_warehoused(self, clip_id: int, scheduled_post_at: str) -> None:
        self.set_clip_field(
            clip_id,
            warehouse_state="warehouse",
            scheduled_post_at=scheduled_post_at,
            refinement_count=0,
        )

    def mark_clip_refined(self, clip_id: int) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE clips SET refinement_count = COALESCE(refinement_count,0)+1, "
                "last_refined_at = ? WHERE id = ?",
                (_now(), clip_id),
            )

    def mark_clip_review_sent(self, clip_id: int, token: str) -> None:
        self.set_clip_field(
            clip_id,
            warehouse_state="pending_review",
            review_sent_at=_now(),
            review_token=token,
        )

    def mark_clip_reviewed(self, clip_id: int, verdict: str, note: str = "") -> None:
        self.set_clip_field(
            clip_id,
            warehouse_state=verdict,    # 'approved' or 'rejected'
            reviewed_at=_now(),
            review_verdict=verdict,
            review_note=note,
        )

    # ----- posts ---------------------------------------------------------
    def add_post(self, clip_id: int, platform: str, scheduled_for: Optional[str], caption: str, hashtags: Iterable[str]) -> int:
        with self.conn() as c:
            c.execute(
                "INSERT INTO posts (clip_id, platform, scheduled_for, caption, hashtags) "
                "VALUES (?, ?, ?, ?, ?)",
                (clip_id, platform, scheduled_for, caption, json.dumps(list(hashtags))),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def set_post_field(self, post_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))

    def mark_post_posted(self, post_id: int, platform_post_id: str, post_url: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE posts SET status='posted', platform_post_id=?, post_url=?, posted_at=? WHERE id=?",
                (platform_post_id, post_url, _now(), post_id),
            )

    def list_due_posts(self, before: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM posts WHERE status='scheduled' AND scheduled_for <= ?",
                (before,),
            ).fetchall()

    # ----- submissions ---------------------------------------------------
    def add_submission(self, post_id: int, campaign_id: int, submitted_url: str) -> int:
        with self.conn() as c:
            c.execute(
                "INSERT INTO submissions (post_id, campaign_id, submitted_url, submission_status, submitted_at) "
                "VALUES (?, ?, ?, 'submitted', ?) "
                "ON CONFLICT(post_id, campaign_id) DO UPDATE SET "
                "submitted_url=excluded.submitted_url, submission_status='submitted', submitted_at=excluded.submitted_at",
                (post_id, campaign_id, submitted_url, _now()),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def set_submission_field(self, submission_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "screenshot_paths" in fields and isinstance(fields["screenshot_paths"], list):
            fields["screenshot_paths"] = json.dumps(fields["screenshot_paths"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE submissions SET {cols} WHERE id=?", (*fields.values(), submission_id))

    def list_submissions_needing_screenshot(self, older_than_iso: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM submissions "
                "WHERE submission_status='submitted' "
                "AND analytics_screenshot_at IS NULL "
                "AND submitted_at <= ?",
                (older_than_iso,),
            ).fetchall()

    # ----- analytics -----------------------------------------------------
    def record_analytics(self, post_id: int, snapshot: dict[str, Any]) -> int:
        with self.conn() as c:
            c.execute(
                "INSERT INTO analytics (post_id, captured_at, views, likes, comments, shares, saves, watch_time_sec, raw_payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    _now(),
                    snapshot.get("views", 0),
                    snapshot.get("likes", 0),
                    snapshot.get("comments", 0),
                    snapshot.get("shares", 0),
                    snapshot.get("saves", 0),
                    snapshot.get("watch_time_sec"),
                    json.dumps(snapshot.get("raw", {})),
                ),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # ----- run_log -------------------------------------------------------
    def log_run(self, module: str, action: str, status: str, message: str = "", target_id: Optional[int] = None) -> int:
        with self.conn() as c:
            c.execute(
                "INSERT INTO run_log (module, action, target_id, status, message, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (module, action, target_id, status, message, _now(), _now()),
            )
            return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
