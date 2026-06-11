#!/usr/bin/env bash
# Pull the latest persistent state into the working tree at the start of a
# GitHub Actions workflow run.
#
# State (SQLite DB + .auth/ session cookies + Gmail OAuth tokens) lives in a
# dedicated `state` branch as an encrypted tar.gz so it survives between
# runs and is *not* visible in the public main branch.
#
# Required env (set by the workflow):
#   STATE_ENCRYPTION_KEY   symmetric key stored as a GitHub Secret
#
# Idempotent: if the state branch doesn't exist yet (first run), creates an
# empty state and continues — bootstrapping a fresh deploy.
set -euo pipefail

: "${STATE_ENCRYPTION_KEY:?STATE_ENCRYPTION_KEY must be set (GitHub Secret)}"

REPO_DIR="$(pwd)"
STATE_BRANCH="${STATE_BRANCH:-state}"
ENCRYPTED_FILE="state.enc"

log() { echo -e "\033[1;36m[state-pull]\033[0m $*"; }

# Try to fetch the state branch.  If it doesn't exist, we're bootstrapping.
git fetch origin "$STATE_BRANCH" --depth 1 2>/dev/null || {
  log "no '$STATE_BRANCH' branch yet — bootstrapping empty state"
  mkdir -p .auth data logs
  exit 0
}

# Checkout the encrypted blob into a tmp dir without polluting the worktree
TMP="$(mktemp -d)"
git --work-tree="$TMP" checkout "origin/$STATE_BRANCH" -- "$ENCRYPTED_FILE" 2>/dev/null || {
  log "state branch exists but has no $ENCRYPTED_FILE — bootstrapping"
  mkdir -p .auth data logs
  exit 0
}

log "decrypting state from origin/$STATE_BRANCH"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
  -pass "env:STATE_ENCRYPTION_KEY" \
  -in "$TMP/$ENCRYPTED_FILE" \
  -out "$TMP/state.tar.gz"

log "extracting into $REPO_DIR"
tar -xzf "$TMP/state.tar.gz" -C "$REPO_DIR"

mkdir -p .auth data logs

# Write a row-count watermark — state_push will refuse to push state that
# regresses any table.  This is the safety net against the state-corruption
# bug where a failed workflow's `if: always()` push wipes good state.
python3 - <<'PY' || true
import json, sqlite3, os
counts = {}
try:
    c = sqlite3.connect("data/clipfarmer.db")
    for tbl in ("campaigns", "clips", "posts", "submissions", "analytics", "source_videos"):
        try:
            counts[tbl] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[tbl] = 0
    c.close()
except Exception as e:
    print(f"[state-pull] watermark: DB not openable ({e}); writing all-zeroes")
    counts = {k: 0 for k in ("campaigns", "clips", "posts", "submissions", "analytics", "source_videos")}
with open(".state_watermark.json", "w") as f:
    json.dump(counts, f)
print(f"[state-pull] watermark: {counts}")
PY

rm -rf "$TMP"
