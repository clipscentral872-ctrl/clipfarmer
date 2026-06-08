# Your personal to-do list — clipfarmer

Things only **you** can do (the system can't). Sorted by impact.

---

## 🔴 Now (unblocks the most growth)

### 1. Create a burner Discord account (~10 min)
Without this, every Clipify/Discord-based marketplace stays manual.
- Open `discord.com` in **incognito**
- Register with email alias: `christo2015.vanloggerenberg+clipfarmer@gmail.com`
  (lands in your normal Gmail, but Discord sees it as new)
- **Verify the email** (Discord locks unverified accounts fast)
- Join **Sx Bot Clipify** (the main directory server) with this burner
- Inside that server, follow their verify-your-socials flow to link your real
  TikTok / IG / YT to the burner's Discord ID — that's the trick that makes
  Clipify credit your views to you, not to "the burner"
- Add these two lines to `.env`:
  ```
  DISCORD_BURNER_EMAIL=...
  DISCORD_BURNER_PASSWORD=...
  ```
- Tell me when done → I'll relaunch the headed Playwright login and we're live

### 2. Submit the live Whop posts that are sitting waiting (~10 min)
Posts the system made but hasn't auto-submitted to Whop yet:
- Substack #43 (blur_pad):
  - https://www.youtube.com/shorts/6sjEvHsWte8
  - https://www.instagram.com/reel/DY9zoxRliEH/
- Jacks #44:
  - https://www.youtube.com/shorts/FRf-Xj5SbVE
  - https://www.instagram.com/reel/DY7RYENDbqu/
- Boxabl #48:
  - https://www.youtube.com/shorts/ER-_N3lNKk8
  - https://www.instagram.com/reel/DY7ctzCjaPY/

For each: go to the campaign page on Whop → submit using both URLs +
the demographics PNG from `data/screenshots/yt_studio/` (already saved).

---

## 🟠 Soon (multiplies earnings)

### 3. Retry the TikTok login (the cooldown is over)
Tomorrow when fresh:
- `python scripts/platform_login.py tiktok`
- This time it should NOT show "max attempts" — wait passed
- Sign in with the TikTok creator account you want clips posted under
- DON'T close the window — let the script auto-detect login + save
- (Optional, if you want completely fresh: try via QR code from your phone instead of email/password — different rate-limiter)

### 4. Join higher-CPM Discord clipping marketplaces
Once burner is set up, these are higher-paying than Clipify:
- **ClipStake** (avg $4/k) — Discord-based, similar Sx Bot flow
- **Vyro** (avg $3/k) — Discord-based
- **ClipAffiliates** — Discord-based
- **Cliprise** (newer, growing) — Discord-based
- **ContentRewards.com** — Web-based, similar to Whop

When you join each one, verify your socials inside their Discord with the
burner. Tell me the server name + drop me the `/clips add` autocomplete
screenshot for each — I'll add it to the marketplace router.

### 5. Make sure the scheduler is running (or schedule it to)
Right now nothing is auto-running. The Brain only learns when:
- `track_analytics.py` runs (every 4hrs in the scheduler)
- `refresh_learnings.py` runs (02:30 local in the scheduler)
- `refresh_proposals.py` runs (02:45)
- `refresh_top_performers.py` runs (03:00) — needed for competitor learner

To start the scheduler in the background:
```
python -m scheduler
```
Leave it running on your machine (or get a tiny Railway VPS later if you want
24/7). For now: starting it before bed = it does all the brain work overnight.

---

## 🟡 Whenever (improves quality, not blocking)

### 6. Reconnect Instagram if the token expires
Long-lived Page tokens last as long as your password doesn't change.
If you reset your Facebook password → the token dies → IG analytics stop.
When that happens, repeat the Graph API Explorer flow (5 min).

### 7. Get YouTube Analytics API approval (optional)
Right now I read YT view counts via the Data API (which works). For
*retention/audience age* data, the **YouTube Analytics API** scope is
needed. That's a separate OAuth scope you'd add. Lower priority — only
useful once the Brain wants demographics, which is several builds away.

### 8. Decide on payout aggregation
Each Whop / Clipify / future marketplace pays separately. Once you have
3+ marketplaces firing, decide:
- Do you want me to build a unified earnings dashboard?
- Or are you OK reading each marketplace's payouts separately?

---

## What I'm waiting on FROM YOU before I can build more

- **Burner Discord creds** → unlocks Clipify direct submission + directory scan + "Brain picks next Discord to test"
- **TikTok login** → unlocks TikTok posting (currently `SKIP_TIKTOK=true`) and TikTok analytics into the Brain
- **A green-light to add more marketplaces** → once joined, paste me the server name + the campaign-details example and I'll wire it in

That's it. Everything else, the system does on its own.
