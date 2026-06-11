#!/usr/bin/env bash
# Push the updated state back to the `state` branch at the END of a workflow
# run.  Encrypts SQLite DB + .auth/ as a single blob committed to the branch.
#
# Required env:
#   STATE_ENCRYPTION_KEY
#   GITHUB_TOKEN          provided automatically by Actions
#
# A workflow-level `concurrency: clipfarmer-state` is required upstream to
# prevent two jobs racing on the state branch.  We do a quick rebase-on-push
# anyway as a belt-and-braces guard.
set -euo pipefail

: "${STATE_ENCRYPTION_KEY:?STATE_ENCRYPTION_KEY must be set}"

STATE_BRANCH="${STATE_BRANCH:-state}"
ENCRYPTED_FILE="state.enc"

log() { echo -e "\033[1;36m[state-push]\033[0m $*"; }

# What we persist.  Videos in data/clips/ and data/downloads/ are NOT
# persisted — they're ephemeral and can be regenerated.
ITEMS=()
[[ -f data/clipfarmer.db ]] && ITEMS+=(data/clipfarmer.db)
[[ -f data/clipfarmer.db-journal ]] && ITEMS+=(data/clipfarmer.db-journal)
# Briefs Markdown files are learning artefacts — small, worth persisting.
[[ -d data/briefs ]] && ITEMS+=(data/briefs)
[[ -d .auth ]] && ITEMS+=(.auth)
# Keep the last week of logs for debugging — strict size budget
if [[ -d logs ]]; then
  find logs -type f -mtime +7 -delete 2>/dev/null || true
  ITEMS+=(logs)
fi

if [[ ${#ITEMS[@]} -eq 0 ]]; then
  log "nothing to persist — skipping"
  exit 0
fi

# Regression check: refuse to push if any tracked table is shorter than the
# watermark written by state_pull.  Catches the case where a workflow's
# Python crashed before doing anything productive but `if: always()` still
# fires Push state — which would otherwise wipe good state from the branch.
if [[ -f .state_watermark.json && -f data/clipfarmer.db ]]; then
  REGRESS=$(python3 - <<'PY'
import json, sqlite3, sys
try:
    old = json.load(open(".state_watermark.json"))
except Exception as e:
    print(f"OK: no watermark ({e})")
    sys.exit(0)
try:
    c = sqlite3.connect("data/clipfarmer.db")
except Exception as e:
    print(f"REGRESS: DB not openable ({e})")
    sys.exit(0)
issues = []
for tbl, old_n in old.items():
    try:
        new_n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    except sqlite3.OperationalError:
        new_n = 0
    if new_n < old_n:
        issues.append(f"{tbl} {old_n}->{new_n}")
c.close()
if issues:
    print("REGRESS: " + ", ".join(issues))
else:
    print("OK")
PY
)
  if [[ "$REGRESS" == REGRESS:* ]]; then
    log "regression detected — REFUSING to push state ($REGRESS)"
    log "this protects the state branch from the corruption bug; investigate the run that produced this DB"
    exit 0
  else
    log "regression check passed ($REGRESS)"
  fi
fi

TMP="$(mktemp -d)"
log "archiving ${ITEMS[*]}"
tar -czf "$TMP/state.tar.gz" "${ITEMS[@]}"

log "encrypting (AES-256-CBC, pbkdf2 100k iters)"
openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -salt \
  -pass "env:STATE_ENCRYPTION_KEY" \
  -in "$TMP/state.tar.gz" \
  -out "$TMP/$ENCRYPTED_FILE"

ARCHIVE_BYTES=$(stat -c %s "$TMP/$ENCRYPTED_FILE")
log "encrypted blob is $ARCHIVE_BYTES bytes"
if [[ $ARCHIVE_BYTES -gt $((90 * 1024 * 1024)) ]]; then
  log "WARNING: blob > 90 MB.  GitHub blob limit is 100 MB.  Consider externalising large logs."
fi

# Build a fresh orphan branch each push to keep history small.  We don't
# need every state revision forever; the latest is what matters.
WORK="$(mktemp -d)"
cd "$WORK"
git init -q
git config user.email "actions@github.com"
git config user.name "Clipfarmer State Bot"
cp "$TMP/$ENCRYPTED_FILE" "$ENCRYPTED_FILE"
git add "$ENCRYPTED_FILE"
git commit -q -m "state @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git branch -m "$STATE_BRANCH"

REPO_SLUG="$GITHUB_REPOSITORY"
git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO_SLUG}.git"
git push -fq origin "$STATE_BRANCH"

log "state pushed to origin/$STATE_BRANCH"
rm -rf "$TMP" "$WORK"
