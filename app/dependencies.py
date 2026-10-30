"""Shared dependencies for user-scoped Supabase access."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, SecretStr

from app.config import Settings
from app.database import SupabaseApi
from app.exceptions import ApiError
from app.rate_limit import SlidingWindowLimiter
from app.schema_auth import AuthenticatedUser

_bearer_scheme = HTTPBearer(auto_error=False, description="Supabase Auth access token")


class RecruiterPrincipal(BaseModel):
    """Server-verified identity and request-scoped bearer token."""

    user: AuthenticatedUser
    access_token: SecretStr

    @property
    def user_id(self) -> UUID:
        return self.user.id


def get_supabase_api(request: Request) -> SupabaseApi:
    """Return a configured request client or fail clearly when credentials are absent."""

    settings: Settings = request.app.state.settings
    return SupabaseApi(settings, request.app.state.http_client)


def get_request_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_current_recruiter(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    api: SupabaseDependency,
) -> RecruiterPrincipal:
    """Verify the token with Supabase Auth; never decode client claims as identity."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "authentication_required", "A valid bearer access token is required")
    user = await api.verify_access_token(credentials.credentials)
    if user.is_anonymous or user.banned_until or user.deleted_at:
        raise ApiError(401, "invalid_access_token", "The access token is invalid or expired")
    limiter: SlidingWindowLimiter = request.app.state.rate_limiter
    allowed, retry_after = limiter.allow(f"user:{user.id}", limit=120, window_seconds=60)
    if not allowed:
        raise ApiError(429, "rate_limited", "Too many requests. Wait before trying again.", retry_after=retry_after)
    return RecruiterPrincipal(user=user, access_token=SecretStr(credentials.credentials))


SupabaseDependency = Annotated[SupabaseApi, Depends(get_supabase_api)]
RecruiterDependency = Annotated[RecruiterPrincipal, Depends(get_current_recruiter)]
SettingsDependency = Annotated[Settings, Depends(get_request_settings)]
