"""End-to-end smoke test for the engine.

Usage:
    python scripts/test_engine.py <youtube_or_other_url> [n_clips]

Produces N captioned 9:16 mp4 clips under data/clips/.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

# Make stdout UTF-8 so emoji in Claude-generated captions don't crash print().
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config import settings
from engine import EnginePipeline


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/test_engine.py <source_url> [n_clips]")
        return 2
    source_url = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else settings.clips_per_source

    pipeline = EnginePipeline()
    clips = pipeline.run(source_url, n_clips=n)

    print(f"\n=== Produced {len(clips)} clip(s) ===")
    for c in clips:
        print(f"  score={c.moment.score:.0f}  {c.moment.start_sec:.1f}-{c.moment.end_sec:.1f}s")
        print(f"    hook: {c.moment.hook_text}")
        print(f"    caption: {c.moment.caption_text}")
        print(f"    hashtags: {c.moment.hashtags}")
        print(f"    file: {c.final_path}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
