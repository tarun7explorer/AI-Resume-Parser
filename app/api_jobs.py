"""Tenant-scoped recruiter job endpoints."""

from uuid import UUID

from fastapi import APIRouter, File, Header, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.crud_auth import require_organization_role
from app.crud_jobs import create_job, get_job as load_job, list_jobs, update_job
from app.crud_screening import get_dashboard
from app.dependencies import RecruiterDependency, SettingsDependency, SupabaseDependency
from app.exceptions import ApiError
from app.job_description import extract_job_description_pdf
from app.schema_jobs import JobCreateRequest, JobResponse, JobSummaryResponse, JobUpdateRequest
from app.schema_screening import DashboardResponse
from app.rate_limit import enforce_request_limit

router = APIRouter(tags=["jobs"])


class JobDescriptionText(BaseModel):
    text: str


@router.get("/organizations/{organization_id}/dashboard", response_model=DashboardResponse)
async def dashboard(
    organization_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> DashboardResponse:
    return await get_dashboard(api, principal, organization_id)


@router.get("/organizations/{organization_id}/jobs", response_model=list[JobSummaryResponse])
async def get_jobs(
    organization_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> list[JobSummaryResponse]:
    return await list_jobs(api, principal, organization_id)


@router.get("/organizations/{organization_id}/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    organization_id: UUID,
    job_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> JobResponse:
    return await load_job(api, principal, organization_id, job_id)


@router.post(
    "/organizations/{organization_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_job(
    organization_id: UUID,
    payload: JobCreateRequest,
    response: Response,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> JobResponse:
    job, replay = await create_job(api, principal, organization_id, idempotency_key, payload)
    if replay:
        response.status_code = status.HTTP_200_OK
    return job


@router.patch("/organizations/{organization_id}/jobs/{job_id}", response_model=JobResponse)
async def patch_job(
    organization_id: UUID,
    job_id: UUID,
    payload: JobUpdateRequest,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> JobResponse:
    return await update_job(api, principal, organization_id, job_id, payload)


@router.post("/organizations/{organization_id}/jobs/parse-description", response_model=JobDescriptionText)
async def parse_job_description_pdf(
    organization_id: UUID,
    request: Request,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    settings: SettingsDependency,
    file: UploadFile = File(),
) -> JobDescriptionText:
    enforce_request_limit(request, f"job-pdf:{principal.user_id}", limit=20, window_seconds=3600)
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise ApiError(400, "invalid_job_description_pdf", "Choose a PDF job description")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise ApiError(400, "invalid_job_description_pdf", "Choose a PDF job description")
    content = await file.read(settings.resume_max_bytes + 1)
    await file.close()
    if not content or len(content) > settings.resume_max_bytes:
        raise ApiError(413, "job_description_too_large", "The PDF exceeds the configured upload limit")
    return JobDescriptionText(text=extract_job_description_pdf(content))
