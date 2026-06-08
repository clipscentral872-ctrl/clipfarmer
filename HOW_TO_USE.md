# How to earn with clipfarmer

What's automated end-to-end:
1. Engine takes a source video → produces N captioned 9:16 clips
2. Smart face-detection cropping (centers on the speaker for single-host content, vertical-stacks two faces for podcasts)
3. Telegram approval gate — every clip waits for your `/approve` or `/reject`
4. Auto-posts approved clips to **YouTube Shorts** + **Instagram Reels** (+ TikTok once you finish that login)

What you still do by hand (for now):
- Find the source video for each campaign (download from the Google Drive resource the campaign provides, or grab a livestream URL)
- Paste each posted clip URL into the Whop campaign's submission form (Title + Video Link + Demographics Image)
- Send 48-hour analytics screenshots to Whop support chat

---

## Day-to-day flow

```powershell
cd C:\Users\chris\clipfarmer
.\.venv\Scripts\Activate.ps1
```

**1. Refresh the campaign list (run once a day or whenever Whop pushes new campaigns):**
```powershell
python -m scanner --debug
```

**1b. Auto-pull each campaign's brief + rules** (Layer 2 — does the rule-reading for you):
```powershell
# Default: only new campaigns missing a brief
python scripts\auto_extract_briefs.py
# Specific campaign:
python scripts\auto_extract_briefs.py --id 43
# Re-pull everything:
python scripts\auto_extract_briefs.py --all --force
```
This drills into each Whop campaign, follows any Google Doc link to the brief, runs Claude over the text, and saves the structured rules to the DB. After this, every later run honours the campaign's exact caption, forbidden phrases, required platforms, etc., automatically.

**1c. (Optional) Auto-download the source footage:**
```powershell
python scripts\download_source.py 43
```
Handles Google Drive single-file links + WeTransfer (via Playwright in your existing logged-in session) + direct video URLs. Falls back to telling you to download manually if the host isn't supported.

**2. See what's in the DB:**
```powershell
python scripts\show_campaigns.py
```

**3. (Optional but recommended) Pull the campaign's top performers** so the scorer mimics what already wins there.

  Easiest — let the scraper do it:
  ```powershell
  python scripts\scrape_top_performers.py <campaign_id>
  # If the program has multiple sub-campaigns, name one:
  python scripts\scrape_top_performers.py <campaign_id> --sub "EnhancedGames Streamer Clips"
  # Add --headed if you want to watch the browser
  ```

  If the scraper misses (Whop's UI shifts), fall back to manual seeding. Make a `top.json`:
  ```json
  [
    {"title":"I tried Enhanced Games drugs for 30 days","views":"847K","platform":"tiktok","length_sec":47,"notes":"cold open w/ bottles, bold-claim hook"},
    {"title":"These steroids are LEGAL?!","views":"1.2M","platform":"instagram","length_sec":32}
  ]
  ```
  Push it onto the campaign:
  ```powershell
  python scripts\seed_top_performers.py <campaign_id> --file top.json
  ```

  Either way, the scorer prompt will now include the winners as style signal for that campaign.

**4. Produce + post clips for one campaign:**
```powershell
python -m orchestrator --campaign <campaign_id> --source "<youtube_url>"
```

You'll get a Telegram message per clip. Reply `/approve` to post or `/reject` to skip.

**5. Submit to Whop manually:**
- Open the Whop campaign page in your browser
- Click "Submit Video"
- Title: anything (or the clip's auto-caption)
- Video Link: the YouTube Shorts URL from Telegram
- Demographics Image: blank for now (`data/screenshots/placeholder-demographics.png` works)

**6. 48 hours later (now automated):**

The system will Telegram-ping you automatically when a post crosses 48 hours, with the latest stats + the Whop support-chat link. Or run it on demand:
```powershell
# Send the 48hr ping for any posts that crossed the line
python scripts\notify_48hr_screenshots.py
# Refresh latest view counts across all posted clips (no Telegram)
python scripts\track_analytics.py
# Just one post
python scripts\track_analytics.py --post 17
```

When the ping arrives, all that's left is:
- Open the platform analytics for that post
- Screenshot the required tabs (YouTube: Overview/Reach/Engagement/Audience; TikTok+IG: Overview/Engagement/Viewers)
- Drop the screenshots into the Whop campaign's Support Chat (URL is in the Telegram message)

---

## Knobs in `.env`

| Var | What it does |
|---|---|
| `REQUIRE_TELEGRAM_APPROVAL=true` | Pause every post for your approval. Set to `false` once you trust it. |
| `MIN_BUDGET_REMAINING_PCT=60` | Skip campaigns under this budget remaining %. |
| `CLIPS_PER_SOURCE=3` | How many clips Claude pulls from one source video. |
| `CLIP_MIN_SECONDS=30` / `CLIP_MAX_SECONDS=60` | Clip length bounds. |
| `WHISPER_MODEL=base` | Bigger = better transcription but slower. `small` is a good upgrade if you have time. |
| `ENABLE_VISION_SCORING=true` | Claude looks at sampled frames from each candidate and re-ranks based on what's visually happening (reactions, action, payoff shots). Set `false` to fall back to transcript-only. |
| `VISION_TEXT_WEIGHT=0.55` / `VISION_VISUAL_WEIGHT=0.45` | How much the final score is dialogue vs. visuals. Bump visual weight if you're clipping mostly action/sport content. |

---

## Hands-free mode (the scheduler)

Designed for the target cadence: 3 campaigns × 2 clips/day = 6 clips/day, spread across 6 slots so YouTube + IG quotas never burst.

```powershell
python -m scheduler
```
Runs forever (Ctrl-C to stop). Fires:
- **Scan** every 6 h (refresh campaigns)
- **Brief pull** at 02:00 + 14:00 (auto_extract_briefs for new campaigns)
- **Post slots** at 09 / 11 / 13 / 15 / 17 / 19 local — each slot produces + posts ONE clip on the next eligible campaign (picker skips campaigns at quota or missing source)
- **Track** every 4 h (refresh view counts)
- **48hr pings** every hour at :15

If you just want one slot now (manual fire / Task Scheduler):
```powershell
python scripts\run_one_slot.py
python scripts\run_one_slot.py --campaign 43
```

**Daily quota:** 2 clips/day per campaign, enforced by the picker. Add a 4th campaign and just bump `DAILY_CLIP_QUOTA` in `scheduler/quota.py` (or add more slot times).

## Coming next (Phase 2)

- **Brain:** learns from tracker data what clip styles earn for each campaign and feeds back into the scorer
- **Top-performer style mimicry:** auto-scrape per-sub-campaign top performers (manual seed + best-effort scraper already in)
- **X (Twitter) publisher**
- **TikTok publisher** (waiting on Chris's TikTok lockout to clear)
