"""Take a short-lived FB User Access Token, find the Instagram Business
Account ID + Page Access Token. Optionally exchange to a long-lived token
if APP_ID + APP_SECRET are provided.

Usage:
    python scripts/instagram_setup.py <short_lived_user_token> [app_id] [app_secret]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

GRAPH = "https://graph.facebook.com/v21.0"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/instagram_setup.py <user_token> [app_id] [app_secret]")
        return 2
    user_token = sys.argv[1]
    app_id = sys.argv[2] if len(sys.argv) > 2 else None
    app_secret = sys.argv[3] if len(sys.argv) > 3 else None

    print("\n[step 1] Listing FB pages this token can manage...")
    r = requests.get(
        f"{GRAPH}/me/accounts",
        params={
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"FAILED ({r.status_code}): {r.text}")
        return 1
    data = r.json()
    pages = data.get("data", [])
    if not pages:
        print("No Facebook Pages found on this account. Make sure the IG account is linked to a Page.")
        return 1

    print(f"Found {len(pages)} page(s):\n")
    ig_account = None
    target_page = None
    for p in pages:
        ig = p.get("instagram_business_account")
        print(f"  Page: {p.get('name')}  (id: {p.get('id')})")
        if ig:
            print(f"    -> Instagram Business Account id: {ig.get('id')}")
            ig_account = ig
            target_page = p

    if not ig_account or not target_page:
        print("\nNo page has a linked Instagram Business Account.")
        print("In the IG mobile app: Profile -> Edit profile -> Page -> select your FB Page.")
        return 1

    page_token = target_page["access_token"]

    print("\n[step 2] Confirming IG account is reachable...")
    r2 = requests.get(
        f"{GRAPH}/{ig_account['id']}",
        params={"fields": "id,username", "access_token": page_token},
        timeout=30,
    )
    if r2.status_code >= 400:
        print(f"  FAILED ({r2.status_code}): {r2.text}")
        return 1
    ig_info = r2.json()
    print(f"  IG account ok: @{ig_info.get('username')}")

    long_lived_user_token = None
    long_lived_page_token = page_token  # default to short-lived page token

    if app_id and app_secret:
        print("\n[step 3] Exchanging user token for a long-lived (60-day) user token...")
        r3 = requests.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": user_token,
            },
            timeout=30,
        )
        if r3.status_code >= 400:
            print(f"  FAILED ({r3.status_code}): {r3.text}")
        else:
            long_lived_user_token = r3.json().get("access_token")
            print("  long-lived user token obtained")

        if long_lived_user_token:
            print("\n[step 4] Re-querying /me/accounts with the long-lived token to get a never-expiring page token...")
            r4 = requests.get(
                f"{GRAPH}/me/accounts",
                params={
                    "access_token": long_lived_user_token,
                    "fields": "id,access_token",
                },
                timeout=30,
            )
            if r4.status_code < 400:
                for p in r4.json().get("data", []):
                    if p["id"] == target_page["id"]:
                        long_lived_page_token = p["access_token"]
                        print("  long-lived page token obtained")
                        break

    print("\n" + "=" * 60)
    print("Drop these into .env:")
    print("=" * 60)
    print(f"INSTAGRAM_USER_ID={ig_account['id']}")
    print(f"INSTAGRAM_ACCESS_TOKEN={long_lived_page_token}")
    if not app_id:
        print("\n(For a non-expiring token, re-run with app_id + app_secret:")
        print(" python scripts/instagram_setup.py <user_token> <app_id> <app_secret>)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
