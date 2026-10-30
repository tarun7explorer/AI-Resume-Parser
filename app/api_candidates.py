"""Tenant-scoped candidate records and authorized private file links."""

from uuid import UUID

from fastapi import APIRouter

from app.crud_candidates import create_candidate, list_candidates
from app.crud_screening import delete_resume, get_resume_download_url
from app.dependencies import RecruiterDependency, SettingsDependency, SupabaseDependency
from app.schema_candidates import (
    CandidateCreateRequest,
    CandidateResponse,
    SignedResumeUrlResponse,
)

router = APIRouter(tags=["candidates"])


@router.get("/organizations/{organization_id}/candidates", response_model=list[CandidateResponse])
async def get_candidates(
    organization_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> list[CandidateResponse]:
    return await list_candidates(api, principal, organization_id)


@router.post("/organizations/{organization_id}/candidates", response_model=CandidateResponse, status_code=201)
async def post_candidate(
    organization_id: UUID,
    payload: CandidateCreateRequest,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> CandidateResponse:
    return await create_candidate(api, principal, organization_id, payload)


@router.get(
    "/organizations/{organization_id}/resumes/{resume_id}/signed-url",
    response_model=SignedResumeUrlResponse,
)
async def get_resume_url(
    organization_id: UUID,
    resume_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
    settings: SettingsDependency,
) -> SignedResumeUrlResponse:
    return await get_resume_download_url(api, principal, organization_id, resume_id, settings)


@router.delete("/organizations/{organization_id}/resumes/{resume_id}", status_code=202)
async def delete_resume_file(
    organization_id: UUID,
    resume_id: UUID,
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> dict[str, str]:
    cleanup_status = await delete_resume(api, principal, organization_id, resume_id)
    return {"status": "deleted", "cleanup_status": cleanup_status}
