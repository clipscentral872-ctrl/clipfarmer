"""Pull realized performance per clip into learning_data.

Joins clips → posts → analytics → submissions to extract:
  - per-feature outcomes (duration bucket, hook style, topic tag, etc.)
  - total views and total earnings per clip
Stores rows into learning_data for the ScoringModel to consume.
"""

from __future__ import annotations

from db import Repository


class Learner:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def harvest(self) -> int:
        """Recompute learning_data for all clips with recent activity.

        Returns the number of learning_data rows written/updated.
        """
        raise NotImplementedError
