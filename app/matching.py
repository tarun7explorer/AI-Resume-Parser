"""Evidence validation and deterministic scoring entry points."""

from app.ai import validate_evidence
from app.scoring import SCORING_VERSION, score_candidate

__all__ = ["SCORING_VERSION", "score_candidate", "validate_evidence"]
