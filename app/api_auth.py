"""Recruiter sign-in and verified-session endpoints."""

import hashlib

from fastapi import APIRouter, Request, Response, status

from app.crud_auth import list_user_organizations
from app.dependencies import RecruiterDependency, SupabaseDependency
from app.schema_auth import (
    CurrentUserResponse,
    OrganizationMembershipResponse,
    RefreshRequest,
    AuthSession,
    SignInRequest,
    TokenResponse,
)
from app.rate_limit import enforce_request_limit

router = APIRouter(tags=["authentication"])


def _token_response(session: AuthSession) -> TokenResponse:
    """Reveal provider tokens only in the response to the session owner."""

    return TokenResponse(
        access_token=session.access_token.get_secret_value(),
        refresh_token=session.refresh_token.get_secret_value(),
        token_type=session.token_type,
        expires_in=session.expires_in,
        user=session.user,
    )


@router.post("/auth/sign-in", response_model=TokenResponse)
async def sign_in(payload: SignInRequest, request: Request, api: SupabaseDependency) -> TokenResponse:
    """Exchange credentials with Supabase Auth; this app stores no passwords."""

    enforce_request_limit(request, "signin:global", limit=120, window_seconds=60)
    account_key = hashlib.sha256(payload.email.strip().casefold().encode("utf-8")).hexdigest()
    enforce_request_limit(request, f"signin:{account_key}", limit=8, window_seconds=300)
    session = await api.sign_in(payload.email, payload.password.get_secret_value())
    return _token_response(session)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_session(payload: RefreshRequest, request: Request, api: SupabaseDependency) -> TokenResponse:
    """Exchange a valid Supabase refresh token for a fresh session."""

    token_key = hashlib.sha256(payload.refresh_token.get_secret_value().encode("utf-8")).hexdigest()
    enforce_request_limit(request, f"refresh:{token_key}", limit=20, window_seconds=300)
    session = await api.refresh_session(payload.refresh_token.get_secret_value())
    return _token_response(session)


@router.get("/auth/me", response_model=CurrentUserResponse)
async def current_user(principal: RecruiterDependency) -> CurrentUserResponse:
    """Return only the identity Supabase Auth verified for this bearer token."""

    return CurrentUserResponse(id=principal.user.id, email=principal.user.email)


@router.post("/auth/sign-out", status_code=status.HTTP_204_NO_CONTENT)
async def sign_out(principal: RecruiterDependency, api: SupabaseDependency) -> Response:
    await api.sign_out(principal.access_token.get_secret_value())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations", response_model=list[OrganizationMembershipResponse])
async def organizations(
    principal: RecruiterDependency,
    api: SupabaseDependency,
) -> list[OrganizationMembershipResponse]:
    """Return tenant memberships visible to the verified recruiter."""

    return await list_user_organizations(api, principal)
