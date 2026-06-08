#!/usr/bin/env bash
# Run this on your LAPTOP (or via WSL on Windows). It pushes the project to
# the Oracle Cloud ARM box, excluding heavy caches and host-specific dirs.
# Usage:
#   VM_IP=1.2.3.4 ./deploy/rsync_from_laptop.sh
# or set VM_IP / SSH_KEY in your shell first.

set -euo pipefail

: "${VM_IP:?set VM_IP=<oracle box public ip>}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oracle_clipfarmer}"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "SSH key not found at $SSH_KEY — set SSH_KEY=/path/to/private.key" >&2
  exit 1
fi

rsync -avz --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'data/downloads/' \
  --exclude 'data/clips/' \
  --exclude 'data/screenshots/' \
  --exclude 'logs/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  ./ "$SSH_USER@$VM_IP:/home/ubuntu/clipfarmer/"

# Push the secrets separately so the include rule above doesn't surprise us.
rsync -avz \
  -e "ssh -i $SSH_KEY" \
  .env "$SSH_USER@$VM_IP:/home/ubuntu/clipfarmer/.env"
rsync -avz \
  -e "ssh -i $SSH_KEY" \
  .auth/ "$SSH_USER@$VM_IP:/home/ubuntu/clipfarmer/.auth/"

echo "✓ Synced.  Now SSH in and run deploy/install.sh:"
echo "  ssh -i $SSH_KEY $SSH_USER@$VM_IP"
echo "  cd ~/clipfarmer && bash deploy/install.sh"
