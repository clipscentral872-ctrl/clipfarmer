#!/usr/bin/env bash
# Provision a Linux box (Hetzner CX23 x86_64 or Oracle Cloud ARM aarch64)
# for clipfarmer.  Idempotent — safe to re-run.  Run as the `ubuntu` user
# (or whichever user owns the project directory).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/clipfarmer}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

log() { echo -e "\033[1;36m[install]\033[0m $*"; }

ARCH="$(uname -m)"
log "Detected architecture: $ARCH"
case "$ARCH" in
  x86_64|aarch64) : ;;
  *) log "WARNING: unsupported arch $ARCH — pip wheels may be unavailable." ;;
esac

log "1/6  apt update + base packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  software-properties-common curl git ffmpeg build-essential \
  libssl-dev libffi-dev libsqlite3-dev sqlite3 \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
  libcairo2 libdrm2 libgtk-3-0

log "2/6  Python 3.12 via deadsnakes PPA"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -y
  sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
fi
"$PYTHON_BIN" --version

if [[ ! -d "$PROJECT_DIR" ]]; then
  log "3/6  $PROJECT_DIR doesn't exist yet — skipping venv setup."
  log "     rsync the project from your laptop first, then re-run this script."
  exit 0
fi

cd "$PROJECT_DIR"

log "3/6  Python venv + pip deps"
if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

log "4/6  Playwright browsers + system deps"
playwright install --with-deps chromium

log "5/6  Required directories"
mkdir -p .auth data/downloads data/clips data/screenshots logs

log "6/6  systemd units"
sudo cp deploy/clipfarmer-scheduler.service /etc/systemd/system/
if [[ -f deploy/clipfarmer-bot.service ]]; then
  sudo cp deploy/clipfarmer-bot.service /etc/systemd/system/
fi
sudo systemctl daemon-reload
sudo systemctl enable --now clipfarmer-scheduler.service
if [[ -f deploy/clipfarmer-bot.service ]]; then
  sudo systemctl enable --now clipfarmer-bot.service
fi

log "DONE.  Check status with:"
log "  systemctl status clipfarmer-scheduler"
log "  journalctl -u clipfarmer-scheduler -f"
