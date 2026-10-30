"""Versioned deterministic scoring with explicit provisional weights."""

from __future__ import annotations

from app.config import Settings
from app.report_models import (
    CandidateAnalysisAI,
    CandidateReport,
    CategoryScore,
    IntegrityFinding,
    JobRequirements,
    ProfileLink,
    RequirementAssessment,
)
from app.resume_parser import ParsedDocument

SCORING_VERSION = "resume-rubric-v1-2026-10"
WEIGHTS = {
    "required_skills": 0.30,
    "experience_projects": 0.25,
    "responsibilities": 0.20,
    "qualifications": 0.10,
    "evidence_quality": 0.15,
}


def _criterion_value(item: RequirementAssessment) -> float:
    if item.status == "missing" or not item.evidence:
        return 0.0
    if item.status == "uncertain":
        return 0.25
    if item.status == "partial":
        return 0.5 if item.certainty == "explicit" else 0.35
    if item.certainty == "explicit":
        return 1.0
    if item.certainty == "inference":
        return 0.65
    return 0.35


def _evidence_value(item: RequirementAssessment) -> float:
    if not item.evidence or item.status == "missing":
        return 0.0
    return {"explicit": 1.0, "inference": 0.65, "uncertain": 0.35}[item.certainty]


def _assessment_index(analysis: CandidateAnalysisAI) -> dict[tuple[str, str], RequirementAssessment]:
    return {(item.category, item.requirement.casefold()): item for item in analysis.assessments}


def score_candidate(
    requirements: JobRequirements,
    analysis: CandidateAnalysisAI,
    document: ParsedDocument,
    links: list[ProfileLink],
    integrity_findings: list[IntegrityFinding],
    settings: Settings,
    content_sha256: str,
) -> CandidateReport:
    index = _assessment_index(analysis)
    requirement_groups = {
        "required_skills": requirements.required_skills,
        "experience_projects": requirements.experience_expectations,
        "responsibilities": requirements.responsibilities,
        "qualifications": requirements.qualifications,
    }
    category_values: dict[str, list[float]] = {}
    category_counts: dict[str, int] = {}
    category_explanations = {
        "required_skills": "Required skills are scored from explicit or qualified partial evidence.",
        "experience_projects": "Relevant experience and projects are scored only against extracted role expectations.",
        "responsibilities": "Evidence is compared to the role responsibilities stated in the job description.",
        "qualifications": "Qualifications are assessed from cited resume text; unmentioned details remain unknown.",
        "evidence_quality": "Evidence quality rewards traceable, explicit source quotes; inferences receive lower credit.",
    }
    for category, criteria in requirement_groups.items():
        values = [
            _criterion_value(index[(category, criterion.casefold())])
            for criterion in criteria
            if (category, criterion.casefold()) in index
        ]
        category_values[category] = values
        category_counts[category] = len(values)
    evidence_values = [_evidence_value(item) for item in analysis.assessments]
    category_values["evidence_quality"] = evidence_values
    category_counts["evidence_quality"] = len(evidence_values)

    applicable = [key for key, values in category_values.items() if values]
    if not applicable:
        raise ValueError("No job requirements are available for deterministic scoring")
    denominator = sum(WEIGHTS[key] for key in applicable)
    scores = {key: sum(values) / len(values) if values else None for key, values in category_values.items()}
    contributions = {
        key: (WEIGHTS[key] * scores[key] / denominator) if scores[key] is not None else 0.0
        for key in WEIGHTS
    }
    normalized = min(1.0, max(0.0, sum(contributions.values())))
    match_score = round(normalized * 10, 2)
    classification_score = round(normalized * 100, 2)

    breakdown = [
        CategoryScore(
            category=key,
            weight=weight,
            raw_score=scores[key],
            weighted_points=round(contributions[key], 6),
            applicable_items=category_counts[key],
            explanation=(
                f"{category_counts[key]} criteria assessed; mean evidence score {scores[key]:.0%}. "
                "Categories with no stated role criteria are excluded and remaining weights are renormalized."
                if scores[key] is not None
                else "No explicit role criteria in this category; excluded from the score rather than treated as candidate failure."
            ),
        )
        for key, weight in WEIGHTS.items()
    ]

    strengths: list[str] = []
    gaps: list[str] = []
    missing_or_uncertain: list[str] = []
    for assessment in analysis.assessments:
        label = assessment.requirement
        if assessment.status == "supported" and assessment.certainty == "explicit":
            strengths.append(f"{label}: {assessment.explanation}")
        elif assessment.status in {"missing", "partial"}:
            gaps.append(f"{label}: {assessment.explanation}")
        if assessment.status in {"missing", "uncertain"} or assessment.certainty in {"inference", "uncertain"}:
            missing_or_uncertain.append(f"{label}: {assessment.status}; {assessment.explanation}")
    if not analysis.education_qualifications:
        missing_or_uncertain.append("No education or qualification facts were extracted; this is unknown, not proof of absence.")
    if not analysis.experience_projects:
        missing_or_uncertain.append("No experience/project facts were extracted with traceable evidence.")

    facts = [fact.statement for fact in (*analysis.experience_projects, *analysis.skills, *analysis.education_qualifications) if fact.certainty == "explicit"]
    summary = " ".join(facts[:3])
    if not summary:
        summary = "No profile facts were extracted with directly traceable evidence. Review the source document."
    classification_band = (
        "priority_review_provisional" if classification_score >= settings.classification_priority_min
        else "considered_provisional" if classification_score >= settings.classification_considered_min
        else "not_suitable_provisional"
    )
    review_notes = [
        f"Page {finding.page}: {finding.explanation} This signal is uncertain and is not a misconduct finding."
        for finding in integrity_findings
    ]
    limitations = [
        "Scores are provisional prioritization aids, not validated cutoffs or hiring decisions.",
        "Missing resume information is reported as unknown and is not proof that the candidate lacks a qualification.",
        "AI extraction may omit or misinterpret information; review all cited source pages.",
        "Profile-link presence does not verify identity, ownership, or authorship. Links are never fetched automatically.",
        "Integrity signals are observable document-layout clues only and do not establish dishonesty or misconduct.",
    ]
    return CandidateReport(
        scoring_version=SCORING_VERSION,
        source={
            "parser": "pymupdf-tesseract-fallback-v1",
            "page_count": document.page_count,
            "ocr_used": document.used_ocr,
            "sha256": content_sha256,
        },
        summary=summary[:1800],
        job_requirements=requirements,
        profile=analysis,
        category_breakdown=breakdown,
        job_match_score=match_score,
        classification_score=classification_score,
        classification_band=classification_band,
        classification_thresholds={
            "considered_min": settings.classification_considered_min,
            "priority_min": settings.classification_priority_min,
        },
        strengths=strengths[:30],
        gaps=gaps[:60],
        missing_or_uncertain=missing_or_uncertain[:80],
        links=links,
        integrity_findings=integrity_findings,
        human_review_notes=review_notes[:60],
        limitations=limitations,
    )
