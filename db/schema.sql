-- clipfarmer database schema (SQLite)
-- All timestamps stored as ISO-8601 strings (UTC).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- campaigns: Whop Content Rewards campaigns we are tracking
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    whop_campaign_id        TEXT    UNIQUE NOT NULL,
    community_id            TEXT    NOT NULL,
    community_name          TEXT,
    title                   TEXT    NOT NULL,
    description             TEXT,
    payout_per_1k_views     REAL,
    payout_currency         TEXT    DEFAULT 'USD',
    min_duration_sec        INTEGER,
    max_duration_sec        INTEGER,
    platforms_required      TEXT,                       -- JSON list, e.g. ["tiktok","youtube","instagram"]
    rules                   TEXT,                       -- raw campaign rules text
    submission_url          TEXT,                       -- form URL or page URL
    status                  TEXT    DEFAULT 'active',   -- active | paused | ended
    -- Viability scoring (Phase 1 filter rules) --------------------------
    budget_total            REAL,                       -- total campaign pot if visible
    budget_remaining        REAL,                       -- remaining $ if visible
    budget_remaining_pct    REAL,                       -- 0..100; only clip if >= MIN_BUDGET_REMAINING_PCT
    min_payout_threshold    REAL,                       -- min $ before payout, if stated
    min_views_for_payout    INTEGER,                    -- min views required to qualify
    approval_rate           REAL,                       -- 0..1 historical approval ratio if shown
    campaign_frequency      TEXT,                       -- e.g. "weekly", "ongoing"
    viability_score         REAL,                       -- our computed 0..100 rank
    top_performers          TEXT,                       -- JSON list of {title, views, est_earnings, platform, url, notes} for style mimicry
    campaign_brief          TEXT,                       -- raw brief text (paste or scraped from Google Doc / Whop page)
    structured_rules        TEXT,                       -- JSON output of rules_extractor: required_caption, forbidden_phrases, platforms_required, source_links, etc.
    discovered_at           TEXT    NOT NULL,
    last_seen_at            TEXT    NOT NULL,
    ends_at                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_viability ON campaigns(viability_score DESC);

-- ---------------------------------------------------------------
-- source_videos: long-form videos a campaign provides for clipping
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_videos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    source_url          TEXT    NOT NULL,
    title               TEXT,
    duration_sec        INTEGER,
    local_path          TEXT,                       -- where yt-dlp saved it
    transcript_path     TEXT,                       -- where whisper output was saved
    download_status     TEXT    DEFAULT 'pending',  -- pending | downloading | done | failed
    transcribe_status   TEXT    DEFAULT 'pending',  -- pending | running | done | failed
    score_status        TEXT    DEFAULT 'pending',  -- pending | running | done | failed
    error               TEXT,
    discovered_at       TEXT    NOT NULL,
    completed_at        TEXT,
    UNIQUE(campaign_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_source_videos_campaign ON source_videos(campaign_id);
CREATE INDEX IF NOT EXISTS idx_source_videos_download_status ON source_videos(download_status);

-- ---------------------------------------------------------------
-- clips: individual clips cut from a source video
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clips (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_video_id     INTEGER NOT NULL REFERENCES source_videos(id) ON DELETE CASCADE,
    campaign_id         INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    start_sec           REAL    NOT NULL,
    end_sec             REAL    NOT NULL,
    duration_sec        REAL    NOT NULL,
    transcript_excerpt  TEXT,
    ai_score            REAL,                       -- Claude's 0-100 score
    ai_reason           TEXT,                       -- why Claude scored this moment high
    hook_text           TEXT,                       -- first-3-seconds overlay
    caption_text        TEXT,                       -- post caption / description
    suggested_hashtags  TEXT,                       -- JSON list
    raw_clip_path       TEXT,                       -- cut + 9:16 formatted, no captions
    final_clip_path     TEXT,                       -- captions + hook overlay applied
    status              TEXT    DEFAULT 'pending',  -- pending | cut | captioned | ready | posted | failed
    error               TEXT,
    created_at          TEXT    NOT NULL,
    ready_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_clips_campaign ON clips(campaign_id);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_clips_ai_score ON clips(ai_score DESC);

-- ---------------------------------------------------------------
-- posts: a single platform post of one clip
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id             INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform            TEXT    NOT NULL,           -- tiktok | youtube | instagram
    platform_post_id    TEXT,                       -- id returned by platform API
    post_url            TEXT,                       -- public URL of the post
    caption             TEXT,
    hashtags            TEXT,
    scheduled_for       TEXT,
    posted_at           TEXT,
    status              TEXT    DEFAULT 'scheduled',-- scheduled | posting | posted | failed
    error               TEXT,
    UNIQUE(clip_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_scheduled_for ON posts(scheduled_for);

-- ---------------------------------------------------------------
-- submissions: a clip submitted to a Whop campaign for payment
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS submissions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id                 INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    campaign_id             INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    submitted_url           TEXT    NOT NULL,
    submission_status       TEXT    DEFAULT 'pending',  -- pending | submitted | approved | rejected | paid
    submitted_at            TEXT,
    analytics_screenshot_at TEXT,                       -- when 48hr screenshot was sent
    screenshot_paths        TEXT,                       -- JSON list of saved screenshots
    payout_amount           REAL,
    payout_currency         TEXT,
    paid_at                 TEXT,
    rejection_reason        TEXT,
    notes                   TEXT,
    UNIQUE(post_id, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(submission_status);

-- ---------------------------------------------------------------
-- analytics: per-post engagement snapshots over time
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    captured_at     TEXT    NOT NULL,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    saves           INTEGER DEFAULT 0,
    watch_time_sec  REAL,
    raw_payload     TEXT                                 -- full JSON dump from platform
);

CREATE INDEX IF NOT EXISTS idx_analytics_post ON analytics(post_id, captured_at);

-- ---------------------------------------------------------------
-- learning_data: signals the brain uses to reweight scoring
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id         INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    feature_name    TEXT    NOT NULL,    -- e.g. "duration_sec", "hook_style", "topic_tag", "ai_score_bucket"
    feature_value   TEXT    NOT NULL,
    outcome_views   INTEGER,
    outcome_earned  REAL,
    captured_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_clip ON learning_data(clip_id);
CREATE INDEX IF NOT EXISTS idx_learning_feature ON learning_data(feature_name, feature_value);

-- ---------------------------------------------------------------
-- run_log: lightweight scheduler / job log for debugging
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module      TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    target_id   INTEGER,
    status      TEXT    NOT NULL,        -- started | ok | error
    message     TEXT,
    started_at  TEXT    NOT NULL,
    ended_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_log_module ON run_log(module, started_at);
