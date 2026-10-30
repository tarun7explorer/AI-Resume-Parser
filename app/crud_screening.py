"""Screening intake and report persistence without performing AI analysis."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from uuid import UUID

from app.config import Settings
from app.crud_auth import require_organization_role
from app.database import SupabaseApi
from app.dependencies import RecruiterPrincipal
from app.exceptions import ApiError
from app.schema_candidates import ResumeIntakeResponse, SignedResumeUrlResponse
from app.schema_screening import DashboardResponse, IntakeRpcResponse, ScreeningReportResponse, ScreeningSummaryResponse


def _summary(row: dict) -> ScreeningSummaryResponse:
    candidate = row.get("candidate") or {}
    job = row.get("job") or {}
    processing = row.get("processing") or {}
    if isinstance(processing, list):
        processing = processing[0] if processing else {}
    return ScreeningSummaryResponse(
        **{key: row[key] for key in (
            "id", "job_id", "candidate_id", "resume_document_id", "status",
            "job_match_score", "classification_score", "score_version", "created_at",
        )},
        candidate_name=candidate.get("full_name"),
        candidate_email=candidate.get("contact_email"),
        job_title=job.get("title"),
        error_code=processing.get("last_error_code"),
        attempt_count=processing.get("attempt_count"),
        max_attempts=processing.get("max_attempts"),
    )


async def create_screening_intake(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    job_id: UUID,
    idempotency_key: UUID,
    candidate_name: str | None,
    candidate_email: str | None,
    original_filename: str,
    content: bytes,
) -> ResumeIntakeResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    token = principal.access_token.get_secret_value()
    content_hash = hashlib.sha256(content).hexdigest()
    rpc_result = await api.rpc(
        "create_screening_intake",
        token,
        {
            "p_organization_id": str(organization_id),
            "p_job_id": str(job_id),
            "p_candidate_name": candidate_name,
            "p_candidate_email": candidate_email,
            "p_original_filename": original_filename,
            "p_file_size_bytes": len(content),
            "p_content_sha256": content_hash,
            "p_idempotency_key": str(idempotency_key),
        },
    )
    if not isinstance(rpc_result, list) or len(rpc_result) != 1 or not isinstance(rpc_result[0], dict):
        raise ApiError(503, "supabase_invalid_response", "Authentication and data services returned an invalid response")
    result = IntakeRpcResponse.model_validate(rpc_result[0])
    if result.resume_status == "deleted":
        raise ApiError(409, "idempotency_conflict", "This intake was already deleted")
    if result.resume_status == "awaiting_upload":
        try:
            await api.upload_resume(token, result.storage_path, content)
        except ApiError:
            # A retry can safely re-submit the same bytes with the same idempotency key.
            try:
                await api.delete_resume_object(token, result.storage_path)
            except ApiError:
                pass
            raise
        # If finalization has a transient failure, leave the object and row pending.
        # Replaying the same idempotency key can re-upload the same hash and finalize it.
        await api.rpc("complete_resume_upload", token, {"p_resume_id": str(result.resume_document_id)})

    return ResumeIntakeResponse(
        screening_id=result.screening_id,
        candidate_id=result.candidate_id,
        resume_id=result.resume_document_id,
        processing_job_id=result.processing_job_id,
        status=result.status,
    )


async def list_screenings(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    job_id: UUID | None = None,
    status_filter: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = 200,
    offset: int = 0,
    search: str | None = None,
) -> list[ScreeningSummaryResponse]:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    params = {
        "select": "id,job_id,candidate_id,resume_document_id,status,job_match_score,classification_score,score_version:scoring_version,created_at,candidate:candidates(full_name,contact_email),job:jobs(title),processing:processing_jobs(last_error_code,attempt_count,max_attempts)",
        "organization_id": f"eq.{organization_id}",
        "deleted_at": "is.null",
        "order": "created_at.desc,id.desc",
        "limit": str(min(max(limit, 1), 500)),
        "offset": str(max(offset, 0)),
    }
    if job_id is not None:
        params["job_id"] = f"eq.{job_id}"
    if status_filter is not None:
        params["status"] = f"eq.{status_filter}"
    if created_after is not None and created_before is not None:
        params["and"] = f"(created_at.gte.{created_after.isoformat()},created_at.lte.{created_before.isoformat()})"
    elif created_after is not None:
        params["created_at"] = f"gte.{created_after.isoformat()}"
    elif created_before is not None:
        params["created_at"] = f"lte.{created_before.isoformat()}"
    if search and search.strip():
        term = re.sub(r"([\\\"%_])", r"\\\1", search.strip()[:100])
        pattern = f'"*{term}*"'
        params["candidate.or"] = f"(full_name.ilike.{pattern},contact_email.ilike.{pattern})"
        params["job.title"] = f"ilike.{pattern}"
        params["or"] = "(candidate.not.is.null,job.not.is.null)"
    rows = await api.select(
        "screening_results",
        principal.access_token.get_secret_value(),
        params=params,
    )
    return [_summary(row) for row in rows]


async def retry_screening(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    screening_id: UUID,
) -> str:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter"})
    try:
        result = await api.rpc(
            "retry_screening_job",
            principal.access_token.get_secret_value(),
            {"p_organization_id": str(organization_id), "p_screening_id": str(screening_id)},
        )
    except ApiError as exc:
        # The RPC reports a stale/ineligible item as P0002. Keep permission and
        # service failures distinct while presenting retry races as a conflict.
        if exc.status_code in {400, 404}:
            raise ApiError(409, "screening_not_retryable", "This screening is no longer eligible for a manual retry") from None
        raise
    if result != "retryable_failure":
        raise ApiError(409, "screening_not_retryable", "This screening is no longer eligible for a manual retry")
    return result


async def get_screening_report(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    screening_id: UUID,
) -> ScreeningReportResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    rows = await api.select(
        "screening_results",
        principal.access_token.get_secret_value(),
        params={
            "select": "id,job_id,candidate_id,resume_document_id,status,job_match_score,classification_score,score_version:scoring_version,created_at,report_json,candidate:candidates(full_name,contact_email),job:jobs(title),resume:resume_documents(original_filename),processing:processing_jobs(last_error_code,attempt_count,max_attempts)",
            "organization_id": f"eq.{organization_id}",
            "id": f"eq.{screening_id}",
            "deleted_at": "is.null",
            "limit": "1",
        },
    )
    if not rows:
        raise ApiError(404, "screening_not_found", "The screening was not found")
    row = rows[0]
    summary = _summary(row)
    return ScreeningReportResponse(
        **summary.model_dump(),
        report=row.get("report_json"),
        original_filename=(row.get("resume") or {}).get("original_filename"),
    )


async def get_dashboard(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
) -> DashboardResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    token = principal.access_token.get_secret_value()
    org_filter = {"organization_id": f"eq.{organization_id}", "deleted_at": "is.null"}
    active_jobs, candidates, screenings, in_progress, completed, attention, recent = await asyncio.gather(
        api.count("jobs", token, params={**org_filter, "status": "eq.active"}),
        api.count("candidates", token, params=org_filter),
        api.count("screening_results", token, params=org_filter),
        api.count("screening_results", token, params={**org_filter, "status": "in.(queued,processing)"}),
        api.count("screening_results", token, params={**org_filter, "status": "eq.completed"}),
        api.count("screening_results", token, params={**org_filter, "status": "in.(retryable_failure,failed)"}),
        list_screenings(api, principal, organization_id, limit=8),
    )
    return DashboardResponse(
        active_jobs=active_jobs,
        candidates=candidates,
        screenings=screenings,
        in_progress=in_progress,
        completed=completed,
        needs_attention=attention,
        recent=recent,
    )


async def get_resume_download_url(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    resume_id: UUID,
    settings: Settings,
) -> SignedResumeUrlResponse:
    await require_organization_role(api, principal, organization_id, {"owner", "admin", "recruiter", "viewer"})
    rows = await api.select(
        "resume_documents",
        principal.access_token.get_secret_value(),
        params={
            "select": "id,storage_path,status,deleted_at,retention_due_at",
            "id": f"eq.{resume_id}",
            "organization_id": f"eq.{organization_id}",
            "deleted_at": "is.null",
            "limit": "1",
        },
    )
    if not rows:
        raise ApiError(404, "resume_not_found", "The resume was not found")
    row = rows[0]
    due = row.get("retention_due_at")
    if row.get("status") != "available" or (
        due and datetime.fromisoformat(due.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    ):
        raise ApiError(404, "resume_not_found", "The resume was not found")
    signed_url = await api.create_signed_resume_url(
        principal.access_token.get_secret_value(),
        row["storage_path"],
        settings.signed_url_ttl_seconds,
    )
    return SignedResumeUrlResponse(signed_url=signed_url, expires_in=settings.signed_url_ttl_seconds)


async def delete_resume(
    api: SupabaseApi,
    principal: RecruiterPrincipal,
    organization_id: UUID,
    resume_id: UUID,
) -> str:
    """Atomically hide the records and enqueue recoverable private-object cleanup."""

    await require_organization_role(api, principal, organization_id, {"owner", "admin"})
    result = await api.rpc(
        "schedule_resume_deletion",
        principal.access_token.get_secret_value(),
        {"p_organization_id": str(organization_id), "p_resume_id": str(resume_id)},
    )
    if result not in {"queued", "processing", "retryable_failure", "completed", "failed"}:
        raise ApiError(503, "cleanup_status_unavailable", "Resume deletion was scheduled, but its cleanup status is unavailable")
    return result
