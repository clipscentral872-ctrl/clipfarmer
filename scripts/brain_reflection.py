"""Weekly Brain self-critique: how well are its predictions calibrated?"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository
from engine.brain.reflection import reflect, notify

report = reflect(Repository())
notify(report)
print(report)
