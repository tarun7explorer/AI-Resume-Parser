"""User-scoped Supabase Auth, PostgREST, and Storage HTTP access.

Requests use the public anon key and the currently authenticated recruiter's
bearer token. This preserves PostgreSQL RLS and avoids service-role bypasses.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.exceptions import ApiError
from app.schema_auth import AuthenticatedUser

logger = logging.getLogger(__name__)
RESUME_BUCKET_ID = "private-resumes"


class SupabaseApi:
    """Thin, pooled HTTP client for Supabase endpoints, always RLS-scoped."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        if settings.supabase_url is None or settings.supabase_anon_key is None:
            raise ApiError(503, "supabase_not_configured", "Authentication and data services are unavailable")
        self._base_url = str(settings.supabase_url).rstrip("/")
        self._anon_key = settings.supabase_anon_key.get_secret_value()
        self._bucket = RESUME_BUCKET_ID
        self._http = http_client

    def _headers(self, access_token: str | None = None, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._anon_key,
            "Accept": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        return_headers: bool = False,
    ) -> Any:
        request_headers = self._headers(access_token)
        if headers:
            request_headers.update(headers)
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                headers=request_headers,
                params=params,
                json=json_body,
                content=content,
            )
        except httpx.HTTPError:
            raise ApiError(503, "supabase_unavailable", "Authentication and data services are temporarily unavailable") from None

        if response.status_code < 400:
            if not response.content:
                return (None, response.headers) if return_headers else None
            try:
                payload = response.json()
                return (payload, response.headers) if return_headers else payload
            except ValueError:
                raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response") from None

        provider_code = ""
        try:
            error_body = response.json()
            provider_code = str(error_body.get("code", "")) if isinstance(error_body, dict) else ""
        except ValueError:
            pass

        if response.status_code in (401, 403) and access_token:
            if response.status_code == 403:
                raise ApiError(403, "permission_denied", "You do not have permission to perform this action")
            raise ApiError(401, "invalid_access_token", "The access token is invalid or expired")
        if response.status_code == 401:
            raise ApiError(503, "supabase_configuration_error", "Authentication and data services are not configured correctly")
        if response.status_code == 403 or provider_code == "42501":
            raise ApiError(403, "permission_denied", "You do not have permission to perform this action")
        if response.status_code == 404 and provider_code not in {"PGRST205", "PGRST202"}:
            raise ApiError(404, "not_found", "The requested record was not found")
        if response.status_code == 409 or provider_code == "23505":
            raise ApiError(409, "conflict", "The request conflicts with an existing record")
        if response.status_code == 429:
            raise ApiError(429, "supabase_rate_limited", "Please retry the request later")
        if response.status_code >= 500 or provider_code in {"PGRST202", "PGRST205"}:
            logger.warning(
                "Supabase returned an unavailable-service response",
                extra={"provider_status": response.status_code, "provider_code": provider_code[:32]},
            )
            raise ApiError(503, "supabase_unavailable", "Authentication and data services are temporarily unavailable")
        raise ApiError(400, "invalid_data_request", "The request could not be processed")

    async def verify_access_token(self, access_token: str) -> AuthenticatedUser:
        payload = await self._send("GET", "/auth/v1/user", access_token=access_token)
        try:
            return AuthenticatedUser.model_validate(payload)
        except ValidationError:
            raise ApiError(401, "invalid_access_token", "The access token is invalid or expired") from None

    async def select(self, table: str, access_token: str, *, params: Mapping[str, str]) -> list[dict[str, Any]]:
        payload = await self._send(
            "GET",
            f"/rest/v1/{quote(table, safe='')}",
            access_token=access_token,
            params=params,
        )
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
        return payload

    async def count(self, table: str, access_token: str, *, params: Mapping[str, str]) -> int:
        _, headers = await self._send(
            "GET",
            f"/rest/v1/{quote(table, safe='')}",
            access_token=access_token,
            params={**params, "select": "id"},
            headers={"Prefer": "count=exact", "Range": "0-0"},
            return_headers=True,
        )
        content_range = headers.get("content-range", "")
        try:
            return int(content_range.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response") from None

    async def insert(self, table: str, access_token: str, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = await self._send(
            "POST",
            f"/rest/v1/{quote(table, safe='')}",
            access_token=access_token,
            json_body=dict(values),
            headers={"Prefer": "return=representation"},
        )
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
        return payload[0]

    async def update(
        self,
        table: str,
        access_token: str,
        filters: Mapping[str, str],
        values: Mapping[str, Any],
        *,
        select: str,
    ) -> list[dict[str, Any]]:
        payload = await self._send(
            "PATCH",
            f"/rest/v1/{quote(table, safe='')}",
            access_token=access_token,
            params={**filters, "select": select},
            json_body=dict(values),
            headers={"Prefer": "return=representation"},
        )
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
        return payload

    async def rpc(self, function: str, access_token: str, values: Mapping[str, Any]) -> Any:
        return await self._send(
            "POST",
            f"/rest/v1/rpc/{quote(function, safe='')}",
            access_token=access_token,
            json_body=dict(values),
        )


class SupabaseWorkerClient:
    """Server-only service-role operations used exclusively by the queue worker."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        if settings.supabase_url is None or settings.supabase_service_role_key is None:
            raise ApiError(503, "worker_not_configured", "The screening worker requires Supabase service credentials")
        self._base_url = str(settings.supabase_url).rstrip("/")
        self._service_key = settings.supabase_service_role_key.get_secret_value()
        self._http = http_client

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def _json(self, path: str, values: dict[str, Any]) -> Any:
        try:
            response = await self._http.post(
                f"{self._base_url}{path}",
                headers=self._headers("application/json"),
                json=values,
                follow_redirects=False,
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
        except httpx.HTTPError:
            raise ApiError(503, "worker_service_unavailable", "The screening worker cannot reach Supabase") from None
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise ApiError(503, "worker_service_unavailable", "The screening worker cannot reach Supabase")
        if response.status_code >= 400:
            provider_code = ""
            try:
                body = response.json()
                provider_code = str(body.get("code", "")) if isinstance(body, dict) else ""
            except ValueError:
                pass
            if response.status_code in (401, 403) or provider_code == "42501":
                raise ApiError(403, "worker_permission_denied", "Supabase rejected worker authorization")
            if provider_code == "40001":
                raise ApiError(409, "worker_lease_lost", "The worker lease is no longer active")
            raise ApiError(400, "worker_service_rejected", "Supabase rejected a screening-worker operation")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid worker response") from None

    async def claim(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        result = await self._json("/rest/v1/rpc/claim_screening_job", {
            "p_worker_id": worker_id,
            "p_lease_seconds": lease_seconds,
        })
        if not isinstance(result, list):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid queue response")
        if not result:
            return None
        if len(result) != 1 or not isinstance(result[0], dict):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid queue response")
        return result[0]

    async def enqueue_expired_resumes(self, limit: int = 100) -> int:
        result = await self._json("/rest/v1/rpc/enqueue_expired_resume_cleanup", {"p_limit": limit})
        if not isinstance(result, int):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid retention response")
        return result

    async def claim_storage_cleanup(self, worker_id: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        result = await self._json("/rest/v1/rpc/claim_storage_cleanup_job", {
            "p_worker_id": worker_id,
            "p_lease_seconds": lease_seconds,
        })
        if not isinstance(result, list):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid cleanup queue response")
        if not result:
            return None
        if len(result) != 1 or not isinstance(result[0], dict):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid cleanup queue response")
        return result[0]

    async def delete_private_resume(self, storage_path: str) -> None:
        bucket = quote("private-resumes", safe="")
        path = quote(storage_path, safe="/")
        try:
            response = await self._http.delete(
                f"{self._base_url}/storage/v1/object/{bucket}/{path}",
                headers=self._headers(),
                follow_redirects=False,
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
        except httpx.HTTPError:
            raise ApiError(503, "storage_cleanup_unavailable", "The private file cleanup service is unavailable") from None
        if response.status_code in (404, 410):
            return
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise ApiError(503, "storage_cleanup_unavailable", "The private file cleanup service is unavailable")
        if response.status_code >= 400:
            raise ApiError(400, "storage_cleanup_rejected", "The private file cleanup request was rejected")

    async def complete_storage_cleanup(self, cleanup_id: str, worker_id: str) -> None:
        await self._json("/rest/v1/rpc/complete_storage_cleanup_job", {
            "p_cleanup_job_id": cleanup_id,
            "p_worker_id": worker_id,
        })

    async def fail_storage_cleanup(self, cleanup_id: str, worker_id: str, code: str, retryable: bool) -> str:
        result = await self._json("/rest/v1/rpc/fail_storage_cleanup_job", {
            "p_cleanup_job_id": cleanup_id,
            "p_worker_id": worker_id,
            "p_error_code": code,
            "p_retryable": retryable,
        })
        if not isinstance(result, str):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid cleanup status")
        return result

    async def download_resume(self, storage_path: str, *, max_bytes: int) -> bytes:
        bucket = quote("private-resumes", safe="")
        path = quote(storage_path, safe="/")
        try:
            response = await self._http.get(
                f"{self._base_url}/storage/v1/object/{bucket}/{path}",
                headers=self._headers(),
                follow_redirects=False,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
        except httpx.HTTPError:
            raise ApiError(503, "worker_storage_unavailable", "The screening worker cannot retrieve the private resume") from None
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise ApiError(503, "worker_storage_unavailable", "The screening worker cannot retrieve the private resume")
        if response.status_code >= 400:
            raise ApiError(404, "worker_resume_not_found", "The private resume is unavailable")
        if len(response.content) > max_bytes:
            raise ApiError(413, "worker_resume_too_large", "The private resume exceeds the configured size limit")
        return response.content

    async def save_job_requirements(self, job_id: str, organization_id: str, requirements: dict, version: str) -> Any:
        return await self._json("/rest/v1/rpc/save_job_requirements", {
            "p_job_id": job_id,
            "p_organization_id": organization_id,
            "p_requirements": requirements,
            "p_version": version,
        })

    async def complete(self, values: dict[str, Any]) -> None:
        await self._json("/rest/v1/rpc/complete_screening_job", values)

    async def fail(self, values: dict[str, Any]) -> str:
        result = await self._json("/rest/v1/rpc/fail_screening_job", values)
        if not isinstance(result, str):
            raise ApiError(503, "worker_invalid_response", "Supabase returned an invalid worker status")
        return result

    async def upload_resume(self, access_token: str, storage_path: str, content: bytes) -> None:
        bucket = quote(self._bucket, safe="")
        path = quote(storage_path, safe="/")
        await self._send(
            "POST",
            f"/storage/v1/object/{bucket}/{path}",
            access_token=access_token,
            content=content,
            headers={"Content-Type": "application/pdf", "x-upsert": "true"},
        )

    async def delete_resume_object(self, access_token: str, storage_path: str) -> None:
        bucket = quote(self._bucket, safe="")
        path = quote(storage_path, safe="/")
        await self._send("DELETE", f"/storage/v1/object/{bucket}/{path}", access_token=access_token)

    async def create_signed_resume_url(self, access_token: str, storage_path: str, expires_in: int) -> str:
        bucket = quote(self._bucket, safe="")
        path = quote(storage_path, safe="/")
        payload = await self._send(
            "POST",
            f"/storage/v1/object/sign/{bucket}/{path}",
            access_token=access_token,
            json_body={"expiresIn": expires_in},
        )
        signed_path = payload.get("signedURL") if isinstance(payload, dict) else None
        if not isinstance(signed_path, str):
            raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
        if signed_path.startswith("https://") or signed_path.startswith("http://"):
            from urllib.parse import urlsplit

            signed_parts = urlsplit(signed_path)
            base_parts = urlsplit(self._base_url)
            if signed_parts.scheme != base_parts.scheme or signed_parts.netloc != base_parts.netloc:
                raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
            return signed_path
        if signed_path.startswith("/storage/v1/"):
            return f"{self._base_url}{signed_path}"
        if signed_path.startswith("/object/"):
            return f"{self._base_url}/storage/v1{signed_path}"
        raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
