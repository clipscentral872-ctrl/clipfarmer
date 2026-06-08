"""Per-feature weights learned from history.

The ClipScorer queries this model to get prior weights (or a prompt
augmentation) before asking Claude to score new moments — so we bias
toward whatever has actually earned in the past.
"""

from __future__ import annotations

from db import Repository


class ScoringModel:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def weights(self) -> dict[str, float]:
        """Return a {feature_name: weight} dict computed from learning_data."""
        raise NotImplementedError

    def render_prompt_hint(self) -> str:
        """Compact natural-language hint to inject into the scorer's Claude prompt.

        Example output:
          "Historical signal: clips 35-50s outperform; hooks framed as questions
           win 1.7x; finance topics earn 2.1x baseline."
        """
        raise NotImplementedError
