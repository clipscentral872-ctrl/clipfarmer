# Clipfarmer — Free GitHub Actions deploy

Runs the whole system on GitHub's free runners. Truly free (public repo = unlimited minutes). Laptop can stay off.

## What you do once (~15 minutes)

1. **Create a GitHub account** at https://github.com/join if you don't have one.

2. **Install GitHub CLI** so we can script the secret upload.
   - Windows: open PowerShell and run `winget install GitHub.cli`
   - Then log in: `gh auth login` → pick "GitHub.com" → "HTTPS" → "Login with web browser"

3. **From the clipfarmer folder**, run the one-shot bootstrap:
   ```bash
   bash deploy/bootstrap_github.sh
   ```
   This creates the repo, uploads your `.env` and a fresh encryption key as GitHub Secrets, and pushes your current SQLite DB + auth cookies as the initial encrypted state. ~3 minutes.

4. **Done.** You can shut your laptop. Workflows fire on their cron schedules.

## What lives where

| Concern | Location |
|---|---|
| Code | Public `main` branch of your GitHub repo |
| SQLite DB + `.auth/*` (sessions, OAuth tokens) | Encrypted blob on `state` branch, decrypted only inside Actions runners |
| `.env` values (API keys, passwords) | GitHub Repository Secret `CLIPFARMER_DOTENV` (encrypted at rest) |
| Encryption key | GitHub Repository Secret `STATE_ENCRYPTION_KEY` (auto-generated, never logged) |
| Videos / clips | Ephemeral in runner tmp — generated, posted, deleted |

## What runs when

| Workflow | Cron | Purpose |
|---|---|---|
| `opportunity_scan.yml` | Every hour @ :15 UTC | Vyro/ClipStake/ClipAffiliates scan for new high-EV campaigns |
| `post_slot.yml` | 6× daily (US ET prime time) | Produce + post one clip per fired slot |
| `nightly.yml` | 00:00 UTC = 02:00 SAST | All learning + Director brief refreshes + competitor analysis |
| `telegram_flush.yml` | 13:00 UTC = 15:00 SAST | Deliver overnight queued Telegram messages + daily briefing |
| `health_check.yml` | 12:30 UTC = 14:30 SAST | Catch broken integrations before your window opens |

## Where to check on things

- All runs: `https://github.com/<you>/clipfarmer/actions`
- Trigger a workflow manually: hit "Run workflow" in the Actions UI, or `gh workflow run <file>.yml`
- Live tail a run: in the Actions UI, click the run → click the job → watch the log stream.

## Updating code later

```bash
git pull        # if there are server-side changes
# edit code locally
git add -A && git commit -m "what changed"
git push        # workflows pick up new code automatically
```

You **don't** need to re-run the bootstrap. Secrets and state persist across pushes.

## Rotating the encryption key (advanced)

If `STATE_ENCRYPTION_KEY` is ever compromised:

```bash
NEW_KEY="$(openssl rand -base64 32)"
# 1. Decrypt current state with the OLD key (run locally with old key in env)
# 2. Update the secret:
echo -n "$NEW_KEY" | gh secret set STATE_ENCRYPTION_KEY
# 3. Re-encrypt + push:
STATE_ENCRYPTION_KEY="$NEW_KEY" bash deploy/state_push.sh
```
