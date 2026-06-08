"""Extract rejection patterns from clips Chris /rejected in Telegram."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.rejection_learning import learn_from_rejections

out = learn_from_rejections(Repository())
print(f"Learned from {len(out)} campaign(s)")
for cid, payload in out.items():
    print(f"  #{cid}: {payload['n_rejections']} rejections → {len(payload['patterns'])} pattern(s)")
    for p in payload['patterns']:
        print(f"    - {p}")
