"""Register Chris's downloaded MrBeast video as the current source for
both Vyro MrBeast campaigns (#49 TT/YT and #50 IG).

Usage: python scripts/register_mrbeast_source.py
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from config import settings


SOURCE_FILE_FROM = Path(r"C:\Users\chris\Downloads\I Survived 7 Days in the Arctic.mp4")
TARGET_NAME = "mrbeast_arctic.mp4"


def main() -> int:
    if not SOURCE_FILE_FROM.exists():
        print(f"❌ Source file not found: {SOURCE_FILE_FROM}")
        return 1

    # Copy into the system's downloads directory so the pipeline can find it
    # by the conventional location.
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    target_path = settings.download_dir / TARGET_NAME
    if not target_path.exists():
        print(f"Copying {SOURCE_FILE_FROM.name} → {target_path} ...")
        shutil.copy2(SOURCE_FILE_FROM, target_path)
    else:
        print(f"Already at {target_path}")
    print(f"  Size: {target_path.stat().st_size / 1_048_576:.1f} MB")

    repo = Repository()
    targets = []
    with repo.conn() as c:
        for vyro_id in (
            "vyro::MrBeast (TT/YT) I Survived 7 Days in the Arctic",
            "vyro::MrBeast (IG) I Survived 7 Days in the Arctic",
        ):
            row = c.execute(
                "SELECT id, title FROM campaigns WHERE whop_campaign_id=?", (vyro_id,)
            ).fetchone()
            if row:
                targets.append(dict(row))

    for camp in targets:
        # Add a source_video row pointing to the local file.
        sv_id = repo.add_source_video(
            campaign_id=camp["id"],
            source_url=str(target_path),
            title="I Survived 7 Days in the Arctic",
        )
        # Mark download_status='done' so the engine knows the file is ready.
        with repo.conn() as c:
            c.execute(
                "UPDATE source_videos SET local_path=?, download_status='done', completed_at=? "
                "WHERE id=?",
                (str(target_path), datetime.now(timezone.utc).isoformat(timespec="seconds"), sv_id),
            )
            c.execute(
                "UPDATE campaigns SET current_source_path=?, last_seen_at=? WHERE id=?",
                (str(target_path),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 camp["id"]),
            )
        print(f"  ✅ #{camp['id']} {camp['title']} → source registered (source_video #{sv_id})")

    print("\nReady. Next:")
    print("  python scripts/run_one_slot.py --campaign 49   # TT/YT clip")
    print("  python scripts/run_one_slot.py --campaign 50   # IG clip")
    print("Or via Telegram bot: 'run a clip for #49'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
