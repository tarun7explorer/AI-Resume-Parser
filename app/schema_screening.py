"""Response contracts for health and shared screening API errors."""

from typing import Literal
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness response; does not claim external services are healthy."""

    status: Literal["ok"]


class ErrorDetail(BaseModel):
    """Sanitized validation detail without echoed submitted values."""

    location: list[str]
    message: str
    type: str


class ErrorBody(BaseModel):
    """Common API error envelope."""

    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ScreeningSummaryResponse(BaseModel):
    id: UUID
    job_id: UUID
    candidate_id: UUID
    resume_document_id: UUID
    status: Literal["queued", "processing", "completed", "retryable_failure", "failed"]
    job_match_score: float | None
    classification_score: float | None
    score_version: str | None
    created_at: datetime
    candidate_name: str | None = None
    candidate_email: str | None = None
    job_title: str | None = None
    error_code: str | None = None
    attempt_count: int | None = None
    max_attempts: int | None = None


class ScreeningPageResponse(BaseModel):
    items: list[ScreeningSummaryResponse]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool


class ScreeningReportResponse(ScreeningSummaryResponse):
    report: dict | None = None
    original_filename: str | None = None


class DashboardResponse(BaseModel):
    active_jobs: int
    candidates: int
    screenings: int
    in_progress: int
    completed: int
    needs_attention: int
    recent: list[ScreeningSummaryResponse]


class IntakeRpcResponse(BaseModel):
    screening_id: UUID
    candidate_id: UUID
    resume_document_id: UUID
    processing_job_id: UUID
    storage_path: str
    resume_status: Literal["awaiting_upload", "available", "deleted"]
    status: Literal["queued", "processing", "completed", "retryable_failure", "failed"] = Field(
        validation_alias="screening_status"
    )
    content_sha256: str
    file_size_bytes: int
    is_replay: bool = False


class OrganizationListResponse(BaseModel):
    organization_id: UUID
    role: Literal["owner", "admin", "recruiter", "viewer"]
