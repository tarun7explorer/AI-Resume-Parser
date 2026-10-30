"""Verified, tenant-scoped organization membership endpoints."""

from fastapi import APIRouter

from app.crud_auth import list_user_organizations
from app.dependencies import RecruiterDependency, SupabaseDependency
from app.schema_auth import OrganizationMembershipResponse

router = APIRouter(tags=["organizations"])


@router.get("/organizations", response_model=list[OrganizationMembershipResponse])
async def organizations(
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> list[OrganizationMembershipResponse]:
    """Return memberships visible to the verified user under RLS."""

    return await list_user_organizations(api, principal)
