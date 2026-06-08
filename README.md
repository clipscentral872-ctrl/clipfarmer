# clipfarmer

Automated end-to-end Whop Content Rewards clip-farming pipeline.

```
   ┌────────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌────────────┐
   │  scanner   │ → │  engine  │ → │ publisher │ → │ tracker  │ → │ submitter  │
   │  (Whop)    │   │ (AI cut) │   │ (3 plats) │   │ (stats)  │   │ (Whop pay) │
   └────────────┘   └──────────┘   └───────────┘   └──────────┘   └────────────┘
                                       ↑                                  │
                                       └────────── brain (learning) ──────┘
```

## What it does

1. **Scan** Whop communities for new Content Rewards campaigns and source videos
2. **Download** the source with `yt-dlp`
3. **Transcribe** with OpenAI Whisper (local)
4. **Score** transcript moments with Claude to find the best 30–60s windows
5. **Cut** with FFmpeg and reformat to 9:16
6. **Caption** with auto-captions + a hook-text overlay on the first 3 seconds
7. **Post** to TikTok, YouTube Shorts, and Instagram Reels on a natural schedule
8. **Submit** the post URL to the Whop campaign form
9. **Screenshot** analytics after 48 hours and send to Whop support chat
10. **Track** earnings and engagement per clip
11. **Learn** which clip styles perform and reweight scoring over time

## Repo layout

```
clipfarmer/
├── scanner/       Whop login + campaign + source-video discovery
├── engine/        download · transcribe · score · cut · caption · format
├── publisher/     Metricool API (TikTok · YouTube Shorts · Instagram Reels)
├── tracker/       per-platform analytics pullers
├── submitter/     Whop submission form + 48hr screenshot sender
├── brain/         scoring model + learning loop
├── db/            SQLite schema + repository
├── config/        env-driven settings
└── scheduler.py   APScheduler-based 24/7 orchestrator
```

## Setup

### 1. Python deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. FFmpeg

Install FFmpeg and make sure `ffmpeg` and `ffprobe` are on PATH (or set
`FFMPEG_PATH`/`FFPROBE_PATH` in `.env`).

Windows: `winget install Gyan.FFmpeg` or download the build from
https://www.gyan.dev/ffmpeg/builds/ and add to PATH.

### 3. Credentials

```powershell
Copy-Item .env.example .env
```

Then fill in:

- **Whop**: account email + password, and a comma-separated list of community slugs to scan
- **Anthropic**: API key for clip scoring + caption generation
- **Metricool**: personal access token + brand id + per-network account ids — Metricool publishes to TikTok, YouTube Shorts, and Instagram Reels through one API
- **Telegram**: bot token + chat id for the approval gate (`REQUIRE_TELEGRAM_APPROVAL=true` by default)

### 4. Database

```powershell
python -m db.migrations init
```

### 5. Run

```powershell
python scheduler.py
```

## API access notes (read before running)

- **Metricool plan**: the API + multi-network scheduling require at minimum a paid Metricool plan with the API add-on enabled. The clipfarmer Metricool account must be separate from any existing marketing-bot Metricool account.
- **Whop ToS**: confirm the specific campaigns you target permit automated submissions before enabling the auto-submit step.
- **Campaign viability gate**: by default the scanner only considers campaigns with `budget_remaining_pct >= 60`. Tune via `MIN_BUDGET_REMAINING_PCT` in `.env`.

## Human-in-the-loop (optional)

Set `REQUIRE_TELEGRAM_APPROVAL=true` to gate posting + submission behind
a Telegram approval message. This mirrors the pattern used in the
Comeback Code system and is a recommended kill switch while tuning.

## Status

Scaffold only. Modules are stubbed and will be implemented in this
order: scanner → engine → publisher → submitter → tracker → brain.
