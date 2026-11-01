"""Strict request/response schemas. Extra fields are rejected."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED)


# ---- errors -----------------------------------------------------------------
class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(StrictModel):
    error: ErrorBody


# ---- submission -------------------------------------------------------------
class SubmissionResponse(StrictModel):
    job_id: UUID
    status: JobStatus
    access_token: str
    status_endpoint: str
    poll_after_seconds: int = Field(ge=1)


# ---- results (persisted JSON by the worker; validated again before serving) --
class CategoryResult(StrictModel):
    key: str | None = None
    name: str = Field(min_length=1, max_length=100)
    score: float = Field(ge=0, le=10, allow_inf_nan=False)
    weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    contribution: float = Field(ge=0, le=10, allow_inf_nan=False)
    status: str | None = None
    nominal_weight: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class EvidenceItem(StrictModel):
    claim: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=2000)
    page: int | None = Field(default=None, ge=1)
    category: str | None = None


class CandidateResult(StrictModel):
    candidate_label: str = Field(min_length=1, max_length=200)
    status: str = "scored"
    score_out_of_10: float | None = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    percentage: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    categories: list[CategoryResult] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    missing_or_uncertain_evidence: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    evidence_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    evidence_coverage_basis: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    weights_normalized: bool | None = None
    not_applicable_categories: list[str] = Field(default_factory=list)
    missing_evidence_categories: list[str] = Field(default_factory=list)
    scoring_version: str | None = None


class ScreeningResult(StrictModel):
    """Shape of screening_jobs.result (JSONB)."""

    results: list[CandidateResult]
    summary: dict[str, int] | None = None
    generated_at: datetime | None = None
    scoring_version: str | None = None


# ---- status -----------------------------------------------------------------
class JobErrorInfo(StrictModel):
    code: str
    message: str


class StatusResponse(StrictModel):
    job_id: UUID
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    poll_after_seconds: int | None = None
    results: list[CandidateResult] | None = None
    error: JobErrorInfo | None = None
