"""Tenant-scoped job persistence through the caller's PostgREST JWT."""

from __future__ import annotations

from uuid import UUID

from app.crud_auth import require_organization_role
from app.database import SupabaseApi
from app.dependencies import RecruiterPrincipal
from app.exceptions import ApiError
from app.schema_jobs import JobCreateRequest, JobResponse, JobSummaryResponse, JobUpdateRequest

JOB_FIELDS = (
    "id,organization_id,title,description,required_skills,preferred_skills,"
    "minimum_experience_years,qualifications,responsibilities,status,created_by,created_at,updated_at"
)
JOB_SUMMARY_FIELDS = "id,organization_id,title,required_skills,status,created_at,updated_at"


async def list_jobs(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
) -> list[JobSummaryResponse]:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    rows = await api.select(
        "jobs",
        principal.access_token.get_secret_value(),
        params={
            "select": JOB_SUMMARY_FIELDS,
            "organization_id": f"eq.{organization_id}",
            "deleted_at": "is.null",
            "order": "created_at.desc",
        },
    )
    return [JobSummaryResponse.model_validate(row) for row in rows]


async def get_job(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    job_id: UUID,
) -> JobResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    rows = await api.select(
        "jobs",
        principal.access_token.get_secret_value(),
        params={
            "select": JOB_FIELDS,
            "id": f"eq.{job_id}",
            "organization_id": f"eq.{organization_id}",
            "deleted_at": "is.null",
            "limit": "1",
        },
    )
    if not rows:
        raise ApiError(404, "job_not_found", "The job was not found")
    return JobResponse.model_validate(rows[0])


async def create_job(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    idempotency_key: UUID,
    payload: JobCreateRequest,
) -> tuple[JobResponse, bool]:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    access_token = principal.access_token.get_secret_value()
    filters = {
        "select": JOB_FIELDS,
        "organization_id": f"eq.{organization_id}",
        "created_by": f"eq.{principal.user_id}",
        "idempotency_key": f"eq.{idempotency_key}",
        "limit": "1",
    }
    existing = await api.select("jobs", access_token, params=filters)
    if existing:
        row = existing[0]
        # Do not store or expose a separate fingerprint; compare the persisted request.
        complete = await api.select(
            "jobs",
            access_token,
            params={**filters, "select": JOB_FIELDS},
        )
        full_row = complete[0] if complete else row
        if any(full_row.get(field) != value for field, value in payload.model_dump().items()):
            raise ApiError(409, "idempotency_conflict", "This idempotency key was already used for a different job")
        return JobResponse.model_validate(full_row), True

    try:
        row = await api.insert(
            "jobs",
            access_token,
            {
                "organization_id": str(organization_id),
                "title": payload.title,
                "description": payload.description,
                "required_skills": payload.required_skills,
                "preferred_skills": payload.preferred_skills,
                "minimum_experience_years": payload.minimum_experience_years,
                "qualifications": payload.qualifications,
                "responsibilities": payload.responsibilities,
                "status": "active",
                "created_by": str(principal.user_id),
                "idempotency_key": str(idempotency_key),
            },
        )
    except ApiError as exc:
        if exc.code != "conflict":
            raise
        retry = await api.select(
            "jobs",
            access_token,
            params={**filters, "select": JOB_FIELDS},
        )
        if not retry:
            raise
        if any(retry[0].get(field) != value for field, value in payload.model_dump().items()):
            raise ApiError(409, "idempotency_conflict", "This idempotency key was already used for a different job") from None
        row = retry[0]
        return JobResponse.model_validate(row), True
    return JobResponse.model_validate(row), False


async def update_job(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    job_id: UUID,
    payload: JobUpdateRequest,
) -> JobResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    rows = await api.update(
        "jobs",
        principal.access_token.get_secret_value(),
        {"id": f"eq.{job_id}", "organization_id": f"eq.{organization_id}", "deleted_at": "is.null"},
        payload.model_dump(),
        select=JOB_FIELDS,
    )
    if not rows:
        raise ApiError(404, "job_not_found", "The job was not found")
    return JobResponse.model_validate(rows[0])
