"""Refresh cross-campaign global pattern blob."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.cross_pattern import refresh

out = refresh(Repository())
print(out)
