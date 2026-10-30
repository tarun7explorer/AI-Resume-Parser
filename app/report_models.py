"""Strict internal contracts for AI extraction and persisted candidate reports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequirementCategory = Literal["required_skills", "experience_projects", "responsibilities", "qualifications"]
ClaimType = Literal["explicit", "inference", "uncertain"]
AssessmentStatus = Literal["supported", "partial", "missing", "uncertain"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JobRequirementsAI(StrictModel):
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    preferred_skills: list[str] = Field(default_factory=list, max_length=40)
    experience_expectations: list[str] = Field(default_factory=list, max_length=20)
    responsibilities: list[str] = Field(default_factory=list, max_length=30)
    qualifications: list[str] = Field(default_factory=list, max_length=20)
    minimum_experience_years: float | None = Field(default=None, ge=0, le=60)


class JobRequirements(StrictModel):
    required_skills: list[str] = Field(max_length=80)
    preferred_skills: list[str] = Field(max_length=80)
    experience_expectations: list[str] = Field(max_length=40)
    responsibilities: list[str] = Field(max_length=60)
    qualifications: list[str] = Field(max_length=40)
    minimum_experience_years: float | None = Field(default=None, ge=0, le=60)


class EvidenceSpan(StrictModel):
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=1200)


class ProfileFact(StrictModel):
    statement: str = Field(min_length=1, max_length=500)
    certainty: ClaimType
    evidence: list[EvidenceSpan] = Field(min_length=1, max_length=4)


class RequirementAssessment(StrictModel):
    category: RequirementCategory
    requirement: str = Field(min_length=1, max_length=500)
    status: AssessmentStatus
    certainty: ClaimType
    explanation: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceSpan] = Field(max_length=4)


class CandidateAnalysisAI(StrictModel):
    skills: list[ProfileFact] = Field(default_factory=list, max_length=60)
    experience_projects: list[ProfileFact] = Field(default_factory=list, max_length=30)
    education_qualifications: list[ProfileFact] = Field(default_factory=list, max_length=20)
    assessments: list[RequirementAssessment] = Field(default_factory=list, max_length=160)


class CategoryScore(StrictModel):
    category: RequirementCategory | Literal["evidence_quality"]
    weight: float = Field(ge=0, le=1)
    raw_score: float | None = Field(default=None, ge=0, le=1)
    weighted_points: float = Field(ge=0, le=1)
    applicable_items: int = Field(ge=0)
    explanation: str = Field(max_length=500)


class IntegrityFinding(StrictModel):
    signal: Literal["very_small_text", "instruction_like_text", "off_page_text"]
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=300)
    confidence: Literal["low", "moderate"]


class ProfileLink(StrictModel):
    platform: Literal["linkedin", "github", "portfolio"]
    status: Literal["found", "not_detected", "malformed"]
    url: str | None = Field(default=None, max_length=2048)


class CandidateReport(StrictModel):
    report_version: Literal["candidate-report-v1"] = "candidate-report-v1"
    scoring_version: str = Field(min_length=1, max_length=64)
    source: dict[str, str | int | bool]
    summary: str = Field(max_length=1800)
    job_requirements: JobRequirements
    profile: CandidateAnalysisAI
    category_breakdown: list[CategoryScore] = Field(min_length=5, max_length=5)
    job_match_score: float = Field(ge=0, le=10)
    classification_score: float = Field(ge=0, le=100)
    classification_band: Literal["not_suitable_provisional", "considered_provisional", "priority_review_provisional"]
    classification_thresholds_provisional: bool = True
    classification_thresholds: dict[str, int]
    strengths: list[str] = Field(max_length=30)
    gaps: list[str] = Field(max_length=60)
    missing_or_uncertain: list[str] = Field(max_length=80)
    links: list[ProfileLink] = Field(max_length=3)
    integrity_findings: list[IntegrityFinding] = Field(max_length=50)
    human_review_notes: list[str] = Field(max_length=60)
    limitations: list[str] = Field(min_length=1, max_length=20)
