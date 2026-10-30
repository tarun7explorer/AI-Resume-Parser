"""Validated recruiter job description and screening criteria contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_list(values: list[str]) -> list[str]:
    clean = []
    seen = set()
    for value in values:
        item = value.strip()
        key = item.casefold()
        if item and key not in seen:
            clean.append(item)
            seen.add(key)
    return clean


class JobWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=100_000)
    required_skills: list[str] = Field(default_factory=list, max_length=50)
    preferred_skills: list[str] = Field(default_factory=list, max_length=50)
    minimum_experience_years: float | None = Field(default=None, ge=0, le=60)
    qualifications: str = Field(default="", max_length=10_000)
    responsibilities: str = Field(default="", max_length=10_000)

    @field_validator("title", "description")
    @classmethod
    def trim_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("This field cannot be blank")
        return normalized

    @field_validator("qualifications", "responsibilities")
    @classmethod
    def trim_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("required_skills", "preferred_skills")
    @classmethod
    def normalize_skills(cls, values: list[str]) -> list[str]:
        if any(len(value) > 120 for value in values):
            raise ValueError("Skill names must be 120 characters or fewer")
        return _clean_list(values)


class JobCreateRequest(JobWriteRequest):
    pass


class JobUpdateRequest(JobWriteRequest):
    pass


class JobResponse(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    description: str
    required_skills: list[str]
    preferred_skills: list[str]
    minimum_experience_years: float | None
    qualifications: str
    responsibilities: str
    status: str
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class JobSummaryResponse(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    required_skills: list[str]
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationResponse(BaseModel):
    id: UUID
    name: str


class OrganizationMembershipView(BaseModel):
    organization_id: UUID
    role: str
    organization: OrganizationResponse
