"""Persistent resume intake, processing status, reports, history, and export APIs."""

from __future__ import annotations

import csv
import io
from pathlib import PurePosixPath
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import ValidationError

from app.crud_screening import create_screening_intake, get_screening_report, list_screenings, retry_screening
from app.dependencies import RecruiterDependency, SettingsDependency, SupabaseDependency
from app.exceptions import ApiError
from app.schema_candidates import CandidateCreateRequest, ResumeIntakeResponse
from app.schema_screening import ScreeningPageResponse, ScreeningReportResponse, ScreeningSummaryResponse
from app.rate_limit import enforce_request_limit

router = APIRouter(tags=["screening"])


def _safe_pdf_filename(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/")
    base_name = PurePosixPath(normalized).name
    if not base_name or len(base_name) > 255 or base_name in {".", ".."}:
        raise ApiError(400, "invalid_resume", "Upload a valid PDF resume")
    if any(ord(char) < 32 for char in base_name) or not base_name.lower().endswith(".pdf"):
        raise ApiError(400, "invalid_resume", "Upload a valid PDF resume")
    return base_name


@router.post(
    "/organizations/{organization_id}/jobs/{job_id}/screenings",
    response_model=ResumeIntakeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_screening(
    organization_id: UUID,
    job_id: UUID,
    request: Request,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    settings: SettingsDependency,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    file: Annotated[UploadFile, File()],
    candidate_name: Annotated[str | None, Form(max_length=200)] = None,
    candidate_email: Annotated[str | None, Form(max_length=254)] = None,
) -> ResumeIntakeResponse:
    """Validate a bounded PDF, persist the queue atomically, then upload privately."""

    enforce_request_limit(request, f"uploads:{principal.user_id}", limit=40, window_seconds=3600)
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise ApiError(400, "invalid_resume", "Upload a valid PDF resume")
    filename = _safe_pdf_filename(file.filename)
    content = await file.read(settings.resume_max_bytes + 1)
    await file.close()
    if not content or len(content) > settings.resume_max_bytes:
        raise ApiError(413, "resume_too_large", "The resume exceeds the configured upload limit")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ApiError(400, "invalid_resume", "The uploaded file is not a complete PDF")

    try:
        candidate = CandidateCreateRequest(
            full_name=candidate_name.strip() if candidate_name and candidate_name.strip() else None,
            contact_email=candidate_email.strip() if candidate_email and candidate_email.strip() else None,
        )
    except ValidationError:
        raise ApiError(422, "validation_error", "Candidate details are invalid") from None
    return await create_screening_intake(
        api,
        principal,
        organization_id,
        job_id,
        idempotency_key,
        candidate.full_name,
        candidate.contact_email,
        filename,
        content,
    )


@router.get(
    "/organizations/{organization_id}/jobs/{job_id}/screenings",
    response_model=list[ScreeningSummaryResponse],
)
async def get_screenings(
    organization_id: UUID,
    job_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> list[ScreeningSummaryResponse]:
    return await list_screenings(api, principal, organization_id, job_id=job_id)


@router.get("/organizations/{organization_id}/screenings", response_model=ScreeningPageResponse)
async def search_screenings(
    organization_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    job_id: UUID | None = None,
    screening_status: str | None = Query(default=None, alias="status", pattern="^(queued|processing|completed|retryable_failure|failed)$"),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    search: str | None = Query(default=None, max_length=100),
) -> ScreeningPageResponse:
    if created_after and created_before and created_after > created_before:
        raise ApiError(422, "invalid_date_range", "The start date must be earlier than the end date")
    rows = await list_screenings(
        api, principal, organization_id, job_id=job_id, status_filter=screening_status,
        created_after=created_after, created_before=created_before, limit=limit + 1,
        offset=offset, search=search,
    )
    return ScreeningPageResponse(items=rows[:limit], offset=offset, limit=limit, has_more=len(rows) > limit)


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet programs may evaluate untrusted strings as formulas.
    stripped = text.lstrip(" \t\r\n")
    if text.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@router.get("/organizations/{organization_id}/screenings/export.csv")
async def export_screenings_csv(
    organization_id: UUID,
    request: Request,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    job_id: UUID | None = None,
    screening_status: str | None = Query(default=None, alias="status", pattern="^(queued|processing|completed|retryable_failure|failed)$"),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    search: str | None = Query(default=None, max_length=100),
) -> Response:
    enforce_request_limit(request, f"exports:{principal.user_id}", limit=15, window_seconds=60)
    if created_after and created_before and created_after > created_before:
        raise ApiError(422, "invalid_date_range", "The start date must be earlier than the end date")
    rows: list[ScreeningSummaryResponse] = []
    while len(rows) <= 5000:
        limit = min(500, 5001 - len(rows))
        batch = await list_screenings(
            api, principal, organization_id, job_id=job_id, status_filter=screening_status,
            created_after=created_after, created_before=created_before,
            limit=limit, offset=len(rows), search=search,
        )
        rows.extend(batch)
        if len(batch) < limit:
            break
    if len(rows) > 5000:
        raise ApiError(413, "export_too_large", "Filter the history to export at most 5,000 screenings at a time")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Candidate", "Email", "Job", "Status", "Match score / 10", "Classification / 100", "Created at", "Screening ID"))
    for row in rows:
        writer.writerow(tuple(_csv_cell(value) for value in (
            row.candidate_name, row.candidate_email, row.job_title, row.status,
            row.job_match_score, row.classification_score, row.created_at.isoformat(), row.id,
        )))
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="screening-history.csv"', "Cache-Control": "no-store"},
    )


@router.get("/organizations/{organization_id}/screenings/{screening_id}", response_model=ScreeningReportResponse)
async def screening_report(
    organization_id: UUID,
    screening_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> ScreeningReportResponse:
    return await get_screening_report(api, principal, organization_id, screening_id)


@router.post("/organizations/{organization_id}/screenings/{screening_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_failed_screening(
    organization_id: UUID,
    screening_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> dict[str, str]:
    state = await retry_screening(api, principal, organization_id, screening_id)
    return {"status": state}
