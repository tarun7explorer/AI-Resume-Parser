"""Response contracts for the temporary, public screening demo."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.report_models import CandidateReport


class DemoCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(max_length=255)
    status: Literal["completed", "failed"]
    message: str = Field(max_length=500)
    report: CandidateReport | None = None
    matched_skills: list[str] = Field(default_factory=list, max_length=80)
    missing_requirements: list[str] = Field(default_factory=list, max_length=80)


class DemoScreeningResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[DemoCandidateResult] = Field(min_length=1, max_length=5)

