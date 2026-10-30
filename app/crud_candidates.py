"""Tenant-scoped candidate records; all reads and writes remain under RLS."""

from __future__ import annotations

from uuid import UUID

from app.crud_auth import require_organization_role
from app.database import SupabaseApi
from app.dependencies import RecruiterPrincipal
from app.schema_candidates import CandidateCreateRequest, CandidateResponse


async def list_candidates(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
) -> list[CandidateResponse]:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    rows = await api.select(
        "candidates",
        principal.access_token.get_secret_value(),
        params={
            "select": "id,organization_id,full_name,contact_email,created_at",
            "organization_id": f"eq.{organization_id}",
            "deleted_at": "is.null",
            "order": "created_at.desc",
        },
    )
    return [CandidateResponse.model_validate(row) for row in rows]


async def create_candidate(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    payload: CandidateCreateRequest,
) -> CandidateResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    row = await api.insert(
        "candidates",
        principal.access_token.get_secret_value(),
        {
            "organization_id": str(organization_id),
            "full_name": payload.full_name.strip() if payload.full_name else None,
            "contact_email": payload.contact_email,
            "created_by": str(principal.user_id),
        },
    )
    return CandidateResponse.model_validate(row)
