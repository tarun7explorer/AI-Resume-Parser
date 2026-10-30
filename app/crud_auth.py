"""Authentication-facing data access; identity comes only from Auth verification."""

from __future__ import annotations

from uuid import UUID

from app.database import SupabaseApi
from app.dependencies import RecruiterPrincipal
from app.exceptions import ApiError
from app.schema_auth import OrganizationMembershipResponse


async def list_user_organizations(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
) -> list[OrganizationMembershipResponse]:
    """List memberships returned by RLS for the verified user."""

    memberships = await api.select(
        "organization_memberships",
        principal.access_token.get_secret_value(),
        params={
            "select": "organization_id,role,organization:organizations(name)",
            "user_id": f"eq.{principal.user_id}",
            "order": "created_at.asc",
        },
    )
    return [
        OrganizationMembershipResponse(
            organization_id=row["organization_id"],
            role=row["role"],
            organization_name=(row.get("organization") or {}).get("name", "Organization"),
        )
        for row in memberships
    ]


async def require_organization_role(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    allowed_roles: set[str],
) -> str:
    """Verify membership in the database under RLS, then apply endpoint role rules."""

    memberships = await api.select(
        "organization_memberships",
        principal.access_token.get_secret_value(),
        params={
            "select": "role",
            "organization_id": f"eq.{organization_id}",
            "user_id": f"eq.{principal.user_id}",
            "limit": "1",
        },
    )
    if not memberships:
        # Use 404 to avoid confirming that another tenant exists.
        raise ApiError(404, "organization_not_found", "The organization was not found")
    role = memberships[0].get("role")
    if role not in {"owner", "admin", "recruiter", "viewer"}:
        raise ApiError(503, "invalid_membership", "Organization access could not be verified")
    if role not in allowed_roles:
        raise ApiError(403, "organization_role_required", "Your organization role cannot perform this action")
    return role
