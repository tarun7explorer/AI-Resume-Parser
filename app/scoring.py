"""Deterministic scoring for Resume-Parser.

This module is pure: no I/O, no AI, no database. The final score is ALWAYS
computed here from validated category scores; an LLM-provided total is never
accepted or consulted.

Policy
------
* Category scores must be finite real numbers in [0, 10] (bools/strings rejected).
* Each category has exactly one status:
    - ASSESSED          a validated score is used.
    - MISSING_EVIDENCE  nothing verifiable was found in the extracted text. The
                        category KEEPS its weight and contributes 0. This is
                        reported as "not found in extracted text", not as proof
                        the candidate lacks the skill.
    - NOT_APPLICABLE    the job genuinely has no such requirement. The weight is
                        removed and remaining weights are renormalised. Only
                        categories in NOT_APPLICABLE_ELIGIBLE may use it, so
                        missing evidence can never be relabelled to raise a score.
* Arithmetic uses Decimal (no binary float drift).
* contribution_i  = score_i * effective_weight_i      (unrounded, exact)
* final_raw       = sum(contribution_i)
* final_score     = final_raw rounded HALF_UP to 1 decimal place
* percentage      = final_score * 10   (an exact integer because of the rule above)
* Displayed contributions are rounded HALF_UP to 2 dp; displayed values may differ
  from final_score by at most 0.005 per category, because final_score is computed
  from unrounded contributions.
* Evidence coverage is separate from suitability (see compute_evidence_coverage).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Iterable

SCORING_VERSION = "1.0.0"

SCORE_MIN = Decimal("0")
SCORE_MAX = Decimal("10")
_ONE_DP = Decimal("0.1")
_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")


class CategoryKey(str, Enum):
    REQUIRED_SKILLS = "required_skills"
    EXPERIENCE_PROJECTS = "experience_projects"
    RESPONSIBILITIES = "responsibilities_capabilities"
    QUALIFICATIONS = "qualifications"
    EVIDENCE_STRENGTH = "evidence_strength"


CATEGORY_LABELS: dict[CategoryKey, str] = {
    CategoryKey.REQUIRED_SKILLS: "Required skills",
    CategoryKey.EXPERIENCE_PROJECTS: "Experience and projects",
    CategoryKey.RESPONSIBILITIES: "Responsibilities/capabilities",
    CategoryKey.QUALIFICATIONS: "Qualifications",
    CategoryKey.EVIDENCE_STRENGTH: "Evidence strength",
}

CATEGORY_WEIGHTS: dict[CategoryKey, Decimal] = {
    CategoryKey.REQUIRED_SKILLS: Decimal("0.30"),
    CategoryKey.EXPERIENCE_PROJECTS: Decimal("0.25"),
    CategoryKey.RESPONSIBILITIES: Decimal("0.20"),
    CategoryKey.QUALIFICATIONS: Decimal("0.10"),
    CategoryKey.EVIDENCE_STRENGTH: Decimal("0.15"),
}

# Only categories a job description can legitimately lack.
NOT_APPLICABLE_ELIGIBLE = frozenset({CategoryKey.RESPONSIBILITIES, CategoryKey.QUALIFICATIONS})

if sum(CATEGORY_WEIGHTS.values()) != Decimal("1.00"):  # pragma: no cover - import-time guard
    raise RuntimeError("Category weights must sum to 1.00")


class CategoryStatus(str, Enum):
    ASSESSED = "assessed"
    MISSING_EVIDENCE = "missing_evidence"
    NOT_APPLICABLE = "not_applicable"


class ScoringError(ValueError):
    """Invalid scoring input. `code` is safe to expose; message contains no resume data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CategoryInput:
    key: CategoryKey
    status: CategoryStatus
    score: float | int | None = None


@dataclass(frozen=True)
class CategoryResult:
    key: str
    name: str
    status: str
    score: float
    nominal_weight: float
    effective_weight: float
    contribution: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "score": self.score,
            "nominal_weight": self.nominal_weight,
            "weight": self.effective_weight,
            "contribution": self.contribution,
        }


@dataclass(frozen=True)
class ScoreResult:
    final_score: float
    percentage: int
    categories: tuple[CategoryResult, ...]
    weights_normalized: bool
    not_applicable: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    scoring_version: str = SCORING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_out_of_10": self.final_score,
            "percentage": self.percentage,
            "categories": [c.to_dict() for c in self.categories],
            "weights_normalized": self.weights_normalized,
            "not_applicable_categories": list(self.not_applicable),
            "missing_evidence_categories": list(self.missing_evidence),
            "scoring_version": self.scoring_version,
        }


def validate_score(value: Any, *, field: str = "score") -> Decimal:
    """Return `value` as an exact Decimal after checking it is finite and in [0, 10]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoringError("score_not_numeric", f"{field} must be a number.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ScoringError("score_not_finite", f"{field} must be finite.")
    dec = Decimal(str(value))
    if dec < SCORE_MIN or dec > SCORE_MAX:
        raise ScoringError("score_out_of_range", f"{field} must be between 0 and 10.")
    return dec


def compute_score(inputs: Iterable[CategoryInput]) -> ScoreResult:
    """Compute the authoritative score from exactly one input per category."""
    items = list(inputs)
    by_key: dict[CategoryKey, CategoryInput] = {}
    for item in items:
        if not isinstance(item, CategoryInput) or not isinstance(item.key, CategoryKey):
            raise ScoringError("category_unknown", "Unknown category input.")
        if item.key in by_key:
            raise ScoringError("category_duplicate", f"Duplicate category: {item.key.value}.")
        by_key[item.key] = item
    missing = [k.value for k in CategoryKey if k not in by_key]
    if missing:
        raise ScoringError("category_missing", f"Missing categories: {', '.join(missing)}.")

    scores: dict[CategoryKey, Decimal] = {}
    for key in CategoryKey:
        item = by_key[key]
        if not isinstance(item.status, CategoryStatus):
            raise ScoringError("status_invalid", f"Invalid status for {key.value}.")
        if item.status is CategoryStatus.ASSESSED:
            if item.score is None:
                raise ScoringError("score_missing", f"{key.value} is assessed but has no score.")
            scores[key] = validate_score(item.score, field=key.value)
        elif item.status is CategoryStatus.MISSING_EVIDENCE:
            if item.score is not None and validate_score(item.score, field=key.value) != 0:
                raise ScoringError(
                    "missing_evidence_with_score",
                    f"{key.value} has missing evidence and must not carry a non-zero score.",
                )
            scores[key] = Decimal("0")
        else:  # NOT_APPLICABLE
            if key not in NOT_APPLICABLE_ELIGIBLE:
                raise ScoringError(
                    "not_applicable_forbidden",
                    f"{key.value} cannot be marked not applicable.",
                )
            if item.score is not None:
                raise ScoringError("not_applicable_with_score", f"{key.value} is N/A and must not have a score.")

    applicable = [k for k in CategoryKey if by_key[k].status is not CategoryStatus.NOT_APPLICABLE]
    weight_total = sum(CATEGORY_WEIGHTS[k] for k in applicable)  # > 0: ineligible categories always apply
    normalized = len(applicable) != len(CategoryKey)

    results: list[CategoryResult] = []
    final_raw = Decimal("0")
    for key in CategoryKey:
        item = by_key[key]
        nominal = CATEGORY_WEIGHTS[key]
        if item.status is CategoryStatus.NOT_APPLICABLE:
            results.append(CategoryResult(key.value, CATEGORY_LABELS[key], item.status.value,
                                          0.0, float(nominal), 0.0, 0.0))
            continue
        effective = nominal / weight_total
        contribution = scores[key] * effective
        final_raw += contribution
        results.append(CategoryResult(
            key.value, CATEGORY_LABELS[key], item.status.value,
            float(scores[key]), float(nominal),
            float(effective.quantize(_FOUR_DP, rounding=ROUND_HALF_UP)),
            float(contribution.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        ))

    final = final_raw.quantize(_ONE_DP, rounding=ROUND_HALF_UP)
    final = min(max(final, SCORE_MIN), SCORE_MAX)
    percentage = int(final * 10)  # exact: final has one decimal place
    return ScoreResult(
        final_score=float(final),
        percentage=percentage,
        categories=tuple(results),
        weights_normalized=normalized,
        not_applicable=tuple(k.value for k in CategoryKey if by_key[k].status is CategoryStatus.NOT_APPLICABLE),
        missing_evidence=tuple(k.value for k in CategoryKey if by_key[k].status is CategoryStatus.MISSING_EVIDENCE),
    )


def compute_evidence_coverage(verified_claims: int, proposed_claims: int) -> float:
    """Share of distinct AI-proposed claims whose quotation was verified in the source text.

    coverage = verified / proposed, rounded HALF_UP to 2 dp; 0.0 when nothing was proposed.
    It measures traceability only and is never an input to the suitability score.
    """
    for name, v in (("verified_claims", verified_claims), ("proposed_claims", proposed_claims)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ScoringError("coverage_invalid", f"{name} must be a non-negative integer.")
    if verified_claims > proposed_claims:
        raise ScoringError("coverage_invalid", "verified_claims cannot exceed proposed_claims.")
    if proposed_claims == 0:
        return 0.0
    ratio = Decimal(verified_claims) / Decimal(proposed_claims)
    return float(ratio.quantize(_TWO_DP, rounding=ROUND_HALF_UP))