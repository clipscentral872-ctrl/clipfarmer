"""Brain: outcome-driven quality improver.

Three pieces:
- analyst.py:   builds per-clip outcome records from posts + analytics
- learnings.py: aggregates outcomes into per-campaign patterns
- advisor.py:   renders learned patterns into prompt-ready advice strings

Public entry points:
    from engine.brain import refresh_learnings, advice_for_campaign
"""

from .analyst import build_outcome_records, ClipOutcome
from .learnings import refresh_learnings, get_learnings
from .advisor import advice_for_campaign
from .cross_campaign import refresh_proposals, compute_performance

__all__ = [
    "build_outcome_records",
    "ClipOutcome",
    "refresh_learnings",
    "get_learnings",
    "advice_for_campaign",
    "refresh_proposals",
    "compute_performance",
]
