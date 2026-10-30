"""Public request and response contracts for recruiter authentication."""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("Enter a valid email address")
        return normalized


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr = Field(min_length=1, max_length=8192)


class AuthenticatedUser(BaseModel):
    """Minimal user identity returned by the verified Supabase Auth endpoint."""

    id: UUID
    email: str | None = None
    is_anonymous: bool = False
    banned_until: str | None = None
    deleted_at: str | None = None


class AuthSession(BaseModel):
    """Sensitive provider session kept secret until explicitly serialized."""

    access_token: SecretStr
    refresh_token: SecretStr
    token_type: Literal["bearer"]
    expires_in: int = Field(gt=0)
    user: AuthenticatedUser


class TokenResponse(BaseModel):
    """Session payload returned to the authenticated client after sign-in."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"]
    expires_in: int
    user: AuthenticatedUser


class CurrentUserResponse(BaseModel):
    id: UUID
    email: str | None


class OrganizationMembershipResponse(BaseModel):
    organization_id: UUID
    role: Literal["owner", "admin", "recruiter", "viewer"]
    organization_name: str
