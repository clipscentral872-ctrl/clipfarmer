"""One-time Gmail OAuth authorization.

Run AFTER you've placed the client_secret.json from Google Cloud Console at
.auth/gmail-client-secret.json. Opens a browser window for consent, saves
the refresh token. After that runs forever — no human in the loop.

Usage:
    python scripts/gmail_oauth.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Force UTF-8 stdout for Windows so the emoji doesn't crash cp1252.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.email_fetcher import _get_service


def main() -> int:
    print("Opening browser for Gmail consent...")
    svc = _get_service()
    profile = svc.users().getProfile(userId="me").execute()
    print(f"\n✅ Authorized for {profile.get('emailAddress')}")
    print(f"   Total messages in inbox: {profile.get('messagesTotal')}")
    print(f"\nRefresh token cached. Future runs are unattended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
