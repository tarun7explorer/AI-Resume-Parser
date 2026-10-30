"""Candidate and resume intake response contracts."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CandidateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=254)

    @field_validator("contact_email")
    @classmethod
    def validate_email_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("Enter a valid email address")
        return normalized


class CandidateResponse(BaseModel):
    id: UUID
    organization_id: UUID
    full_name: str | None
    contact_email: str | None
    created_at: datetime


class ResumeIntakeResponse(BaseModel):
    screening_id: UUID
    candidate_id: UUID
    resume_id: UUID
    processing_job_id: UUID
    status: str


class SignedResumeUrlResponse(BaseModel):
    signed_url: str
    expires_in: int
