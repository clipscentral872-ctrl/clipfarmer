#!/usr/bin/env bash
# ONE-SHOT bootstrap for the GitHub Actions deploy.
# Run this ONCE on your laptop after:
#   1. You've installed the GitHub CLI (`gh`) and logged in: `gh auth login`
#   2. The repo exists (this script will create it if it doesn't yet)
#
# What it does:
#   1. Generates a random STATE_ENCRYPTION_KEY (32 bytes, base64) and stores it
#      as a GitHub repository Secret.
#   2. Reads your local .env and stores its FULL contents as the
#      CLIPFARMER_DOTENV repository Secret.
#   3. Pushes your current code to `main`.
#   4. Encrypts + pushes the initial state (SQLite DB + .auth/) to the `state`
#      branch so the first workflow run has something to work with.
#
# After this completes:
#   - Workflows fire on their schedules.
#   - You can shut your laptop.

set -euo pipefail

REPO_NAME="${REPO_NAME:-clipfarmer}"
VISIBILITY="${VISIBILITY:-public}"

log() { echo -e "\033[1;36m[bootstrap]\033[0m $*"; }
die() { echo -e "\033[1;31m[bootstrap ERROR]\033[0m $*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "GitHub CLI not installed. Run: winget install GitHub.cli  (or https://cli.github.com/)"
command -v openssl >/dev/null 2>&1 || die "openssl required (Git for Windows / WSL provide it)."
[[ -f .env ]] || die "no .env in current directory — run this from your clipfarmer/ root."

log "1/5  Authenticating with GitHub..."
gh auth status >/dev/null 2>&1 || die "Not logged in. Run: gh auth login"
OWNER="$(gh api user --jq .login)"
log "      logged in as $OWNER"

log "2/5  Ensuring repo $OWNER/$REPO_NAME exists ($VISIBILITY)..."

# Make this folder a git repo if it isn't already.
if [[ ! -d .git ]]; then
  log "      initialising local git repo"
  git init -q
  git checkout -q -b main 2>/dev/null || git branch -m main
fi

# Identity for any commits this script makes
if [[ -z "$(git config user.email 2>/dev/null || true)" ]]; then
  USER_EMAIL="$(gh api user --jq '.email // empty')"
  USER_NAME="$(gh api user --jq '.name // .login')"
  [[ -z "$USER_EMAIL" ]] && USER_EMAIL="$OWNER@users.noreply.github.com"
  git config user.email "$USER_EMAIL"
  git config user.name "$USER_NAME"
fi

# Commit current code if there isn't a HEAD yet
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  log "      creating initial commit (respecting .gitignore)"
  git add -A
  git commit -q -m "initial clipfarmer commit"
fi

if ! gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  gh repo create "$OWNER/$REPO_NAME" --"$VISIBILITY" --source=. --remote=origin --push
  log "      repo created and initial push done"
else
  log "      repo already exists — pushing latest code"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin "$(git symbolic-ref --short HEAD)"
fi

log "3/5  Generating + uploading STATE_ENCRYPTION_KEY..."
STATE_KEY="$(openssl rand -base64 32)"
echo -n "$STATE_KEY" | gh secret set STATE_ENCRYPTION_KEY --repo "$OWNER/$REPO_NAME"
log "      done"

log "4/5  Uploading CLIPFARMER_DOTENV (full .env)..."
gh secret set CLIPFARMER_DOTENV --repo "$OWNER/$REPO_NAME" < .env
log "      done"

log "5/5  Encrypting + pushing initial state branch..."
export STATE_ENCRYPTION_KEY="$STATE_KEY"
export GITHUB_TOKEN="$(gh auth token)"
export GITHUB_REPOSITORY="$OWNER/$REPO_NAME"
bash deploy/state_push.sh

log "ALL DONE."
log ""
log "Workflows live at: https://github.com/$OWNER/$REPO_NAME/actions"
log "First scheduled run will happen at the next cron tick."
log "Fire one immediately with: gh workflow run opportunity_scan.yml --repo $OWNER/$REPO_NAME"
log ""
log "You can now shut your laptop. The system runs on GitHub's runners."
