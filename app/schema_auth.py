"""Public request and response contracts for recruiter authentication."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class AuthenticatedUser(BaseModel):
    """Minimal user identity returned by the verified Supabase Auth endpoint."""

    id: UUID
    email: str | None = None
    is_anonymous: bool = False
    banned_until: str | None = None
    deleted_at: str | None = None


class OrganizationMembershipResponse(BaseModel):
    organization_id: UUID
    role: Literal["owner", "admin", "recruiter", "viewer"]
    organization_name: str
