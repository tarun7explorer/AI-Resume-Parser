"""Scenario 3: deterministic scoring math (unit level; golden_dataset tests are separate)."""
import math
import json
from pathlib import Path

import pytest

from app.scoring import (
    CATEGORY_WEIGHTS, CategoryInput as CI, CategoryKey as K, CategoryStatus as S, ScoringError,
    compute_evidence_coverage, compute_score, validate_score,
)


def all_assessed(**over):
    base = {k: 5.0 for k in K}
    base.update({K(k): v for k, v in over.items()})
    return [CI(k, S.ASSESSED, v) for k, v in base.items()]


def test_weights_sum_to_one_and_match_spec():
    assert sum(CATEGORY_WEIGHTS.values()) == 1
    assert [float(CATEGORY_WEIGHTS[k]) for k in K] == [0.30, 0.25, 0.20, 0.10, 0.15]


def test_contributions_and_final():
    r = compute_score(all_assessed(required_skills=8, experience_projects=7, responsibilities_capabilities=6,
                                   qualifications=9, evidence_strength=7))
    c = {x.key: x for x in r.categories}
    assert c["required_skills"].contribution == 2.40
    assert c["experience_projects"].contribution == 1.75
    assert c["responsibilities_capabilities"].contribution == 1.20
    assert c["qualifications"].contribution == 0.90
    assert c["evidence_strength"].contribution == 1.05
    assert r.final_score == 7.3 and r.percentage == 73 and not r.weights_normalized


@pytest.mark.parametrize("v,exp", [(0, 0.0), (10, 10.0)])
def test_boundaries(v, exp):
    r = compute_score([CI(k, S.ASSESSED, v) for k in K])
    assert r.final_score == exp and r.percentage == int(exp * 10)


@pytest.mark.parametrize("bad", [-0.01, 10.01, float("nan"), float("inf"), -math.inf, "8", None, True, [], 1e9])
def test_invalid_scores_rejected(bad):
    with pytest.raises(ScoringError):
        validate_score(bad)
    with pytest.raises(ScoringError):
        compute_score(all_assessed(required_skills=bad))


def test_rounding_half_up():
    # 0.25*? choose scores so raw = 6.25 exactly -> 6.3 (half-up, not banker's)
    r = compute_score([CI(K.REQUIRED_SKILLS, S.ASSESSED, 6.25), CI(K.EXPERIENCE_PROJECTS, S.ASSESSED, 6.25),
                       CI(K.RESPONSIBILITIES, S.ASSESSED, 6.25), CI(K.QUALIFICATIONS, S.ASSESSED, 6.25),
                       CI(K.EVIDENCE_STRENGTH, S.ASSESSED, 6.25)])
    assert r.final_score == 6.3 and r.percentage == 63


def test_not_applicable_renormalises():
    inputs = all_assessed(required_skills=10, experience_projects=10, responsibilities_capabilities=10,
                          evidence_strength=10)
    inputs = [i for i in inputs if i.key is not K.QUALIFICATIONS] + [CI(K.QUALIFICATIONS, S.NOT_APPLICABLE)]
    r = compute_score(inputs)
    assert r.weights_normalized and r.not_applicable == ("qualifications",)
    assert r.final_score == 10.0
    assert abs(sum(c.effective_weight for c in r.categories) - 1.0) < 1e-3


@pytest.mark.parametrize("key", [K.REQUIRED_SKILLS, K.EXPERIENCE_PROJECTS, K.EVIDENCE_STRENGTH])
def test_not_applicable_forbidden_for_core_categories(key):
    inputs = [CI(k, S.ASSESSED, 5) for k in K if k is not key] + [CI(key, S.NOT_APPLICABLE)]
    with pytest.raises(ScoringError) as e:
        compute_score(inputs)
    assert e.value.code == "not_applicable_forbidden"


def test_missing_evidence_keeps_weight_and_scores_zero():
    inputs = [CI(k, S.ASSESSED, 10) for k in K if k is not K.REQUIRED_SKILLS] + [CI(K.REQUIRED_SKILLS, S.MISSING_EVIDENCE)]
    r = compute_score(inputs)
    assert not r.weights_normalized and r.missing_evidence == ("required_skills",)
    assert r.final_score == 7.0 and r.percentage == 70  # 10*(1-0.30)


def test_missing_evidence_cannot_carry_score():
    inputs = [CI(k, S.ASSESSED, 5) for k in K if k is not K.QUALIFICATIONS] + [CI(K.QUALIFICATIONS, S.MISSING_EVIDENCE, 4)]
    with pytest.raises(ScoringError):
        compute_score(inputs)


def test_missing_evidence_strictly_below_inapplicable_gaming():
    base = [CI(k, S.ASSESSED, 8) for k in (K.REQUIRED_SKILLS, K.EXPERIENCE_PROJECTS, K.EVIDENCE_STRENGTH, K.RESPONSIBILITIES)]
    miss = compute_score(base + [CI(K.QUALIFICATIONS, S.MISSING_EVIDENCE)])
    na = compute_score(base + [CI(K.QUALIFICATIONS, S.NOT_APPLICABLE)])
    assert miss.final_score < na.final_score


def test_structure_errors():
    with pytest.raises(ScoringError):
        compute_score(all_assessed()[:-1])
    with pytest.raises(ScoringError):
        compute_score(all_assessed() + [CI(K.REQUIRED_SKILLS, S.ASSESSED, 1)])
    with pytest.raises(ScoringError):
        compute_score([CI(k, S.ASSESSED, None) for k in K])


@pytest.mark.parametrize("scores", [(0, 0, 0, 0, 0), (1.1, 2.2, 3.3, 4.4, 5.5), (9.99, 0.01, 5.55, 7.77, 3.33), (10, 9.5, 0.5, 2.25, 8.75)])
def test_percentage_reconciles(scores):
    r = compute_score([CI(k, S.ASSESSED, s) for k, s in zip(K, scores)])
    assert r.percentage == round(r.final_score * 10)
    assert abs(sum(c.contribution for c in r.categories) - r.final_score) <= 0.005 * 5 + 0.05
    assert 0 <= r.final_score <= 10


def test_deterministic():
    a = compute_score(all_assessed(required_skills=7.3)).to_dict()
    assert a == compute_score(all_assessed(required_skills=7.3)).to_dict()


def test_coverage():
    assert compute_evidence_coverage(6, 7) == 0.86
    assert compute_evidence_coverage(0, 0) == 0.0
    assert compute_evidence_coverage(3, 3) == 1.0
    for bad in [(4, 3), (-1, 3), (1.0, 3), (True, 3)]:
        with pytest.raises(ScoringError):
            compute_evidence_coverage(*bad)


def test_synthetic_golden_dataset():
    dataset = json.loads((Path(__file__).parent / "golden_dataset" / "expected.json").read_text(encoding="utf-8"))
    assert dataset["synthetic_only"] is True
    for case in dataset["cases"]:
        not_applicable = set(case["not_applicable"])
        inputs = [
            CI(key, S.NOT_APPLICABLE) if key.value in not_applicable else
            CI(key, S.ASSESSED, case["category_scores"][key.value])
            for key in K
        ]
        result = compute_score(inputs)
        assert result.final_score == case["score_out_of_10"]
        assert result.percentage == case["percentage"]
