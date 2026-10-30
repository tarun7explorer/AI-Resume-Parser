"""Durable Supabase queue worker for validated candidate reports."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Any

import httpx
from pydantic import ValidationError

from app.ai import AIProviderError, GroqProvider
from app.config import Settings
from app.database import SupabaseWorkerClient
from app.exceptions import ApiError
from app.integrity import analyze_integrity
from app.links import extract_contact, extract_profile_links
from app.report_models import JobRequirements
from app.resume_parser import DocumentProcessingError, extract_pdf
from app.scoring import SCORING_VERSION, score_candidate

logger = logging.getLogger(__name__)
JOB_REQUIREMENTS_VERSION = "job-requirements-v1"


def _job_requirements(job: dict[str, Any]) -> JobRequirements:
    cached = job.get("ai_requirements")
    if cached is not None and job.get("ai_requirements_version") == JOB_REQUIREMENTS_VERSION:
        try:
            return JobRequirements.model_validate(cached)
        except (ValidationError, TypeError):
            logger.warning("Ignoring invalid cached job requirements", extra={"job_id": str(job.get("job_id", ""))})
    raise ValueError("job requirements must be extracted")


def _ensure_scorable(requirements: JobRequirements) -> None:
    if not any((
        requirements.required_skills,
        requirements.experience_expectations,
        requirements.responsibilities,
        requirements.qualifications,
    )):
        raise AIProviderError(
            "job_requirements_missing",
            "Add at least one explicit skill, experience, responsibility, or qualification requirement before screening.",
            retryable=False,
        )


async def process_claimed_job(
    job: dict[str, Any],
    worker_id: str,
    settings: Settings,
    client: SupabaseWorkerClient,
    provider: GroqProvider,
) -> None:
    """Process one leased item; report only stable safe error codes to the queue."""

    processing_id = str(job.get("processing_job_id", ""))
    try:
        required_keys = ("organization_id", "job_id", "storage_path", "content_sha256")
        if not processing_id or any(not job.get(key) for key in required_keys):
            raise DocumentProcessingError("queue_record_invalid", "The queued screening record is incomplete.")

        requirements = None
        try:
            requirements = _job_requirements(job)
        except ValueError:
            requirements = await provider.extract_job_requirements(
                title=str(job.get("job_title") or ""),
                description=str(job.get("job_description") or ""),
                required_skills=list(job.get("required_skills") or []),
                preferred_skills=list(job.get("preferred_skills") or []),
                experience_years=job.get("minimum_experience_years"),
                qualifications=str(job.get("qualifications") or ""),
                responsibilities=str(job.get("responsibilities") or ""),
            )
            _ensure_scorable(requirements)
            saved = await client.save_job_requirements(
                str(job["job_id"]), str(job["organization_id"]),
                requirements.model_dump(mode="json"), JOB_REQUIREMENTS_VERSION,
            )
            if saved is not None:
                requirements = JobRequirements.model_validate(saved)
        _ensure_scorable(requirements)

        content = await client.download_resume(str(job["storage_path"]), max_bytes=settings.resume_max_bytes)
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != str(job["content_sha256"]).lower():
            raise DocumentProcessingError("resume_integrity_mismatch", "The stored resume did not match its intake checksum.")
        document = extract_pdf(content, settings, mime_type="application/pdf")
        contact = extract_contact(document.text)
        links = extract_profile_links(document.text)
        integrity = analyze_integrity(document)
        analysis = await provider.extract_candidate(document, requirements)
        report = score_candidate(
            requirements, analysis, document, links, integrity, settings, actual_hash,
        )
        await client.complete({
            "p_processing_job_id": processing_id,
            "p_worker_id": worker_id,
            "p_report": report.model_dump(mode="json"),
            "p_job_match_score": report.job_match_score,
            "p_classification_score": report.classification_score,
            "p_scoring_version": SCORING_VERSION,
            "p_ai_provider": "groq",
            "p_ai_model": settings.groq_model,
            "p_candidate_name": None,
            "p_candidate_email": contact["email"],
        })
        logger.info("Screening completed", extra={"processing_job_id": processing_id})
    except (DocumentProcessingError, AIProviderError, ApiError) as exc:
        code = exc.code if isinstance(exc, (DocumentProcessingError, AIProviderError)) else exc.code
        retryable = exc.retryable if isinstance(exc, (DocumentProcessingError, AIProviderError)) else exc.status_code >= 500
        await _persist_failure(client, processing_id, worker_id, code, retryable)
    except Exception as exc:
        # Avoid exception strings and tracebacks: they may contain document text or credentials.
        logger.error("Unexpected screening worker failure", extra={
            "processing_job_id": processing_id,
            "exception_type": type(exc).__name__,
        })
        await _persist_failure(client, processing_id, worker_id, "screening_processing_failed", True)


async def _persist_failure(
    client: SupabaseWorkerClient,
    processing_id: str,
    worker_id: str,
    code: str,
    retryable: bool,
) -> None:
    safe_code = code if code.replace("_", "").isalnum() and len(code) <= 64 else "screening_processing_failed"
    try:
        state = await client.fail({
            "p_processing_job_id": processing_id,
            "p_worker_id": worker_id,
            "p_error_code": safe_code,
            "p_retryable": retryable,
        })
        logger.warning("Screening processing failed", extra={
            "processing_job_id": processing_id,
            "error_code": safe_code,
            "queue_state": state,
        })
    except ApiError as exc:
        logger.warning("Unable to persist screening failure", extra={
            "processing_job_id": processing_id,
            "error_code": safe_code,
            "service_code": exc.code,
        })


async def process_cleanup_job(
    job: dict[str, Any],
    worker_id: str,
    client: SupabaseWorkerClient,
) -> None:
    cleanup_id = str(job.get("cleanup_job_id", ""))
    try:
        storage_path = str(job.get("storage_path", ""))
        if not cleanup_id or not storage_path or storage_path.startswith("/") or ".." in storage_path.split("/"):
            raise ApiError(400, "cleanup_record_invalid", "The private cleanup record is invalid")
        await client.delete_private_resume(storage_path)
        await client.complete_storage_cleanup(cleanup_id, worker_id)
        logger.info("Private resume cleanup completed", extra={"cleanup_job_id": cleanup_id})
    except ApiError as exc:
        safe_code = exc.code if exc.code.replace("_", "").isalnum() else "storage_cleanup_failed"
        try:
            state = await client.fail_storage_cleanup(
                cleanup_id, worker_id, safe_code, exc.status_code >= 500,
            )
            logger.warning("Private resume cleanup failed", extra={
                "cleanup_job_id": cleanup_id,
                "error_code": safe_code,
                "queue_state": state,
            })
        except ApiError as failure:
            logger.warning("Unable to persist private cleanup failure", extra={
                "cleanup_job_id": cleanup_id,
                "service_code": failure.code,
            })
    except Exception as exc:
        logger.error("Unexpected private cleanup failure", extra={
            "cleanup_job_id": cleanup_id,
            "exception_type": type(exc).__name__,
        })
        try:
            await client.fail_storage_cleanup(cleanup_id, worker_id, "storage_cleanup_failed", True)
        except ApiError:
            pass


async def worker_loop(settings: Settings, http_client: httpx.AsyncClient) -> None:
    """Poll the durable queue until application shutdown."""

    worker_id = f"{uuid.uuid4()}"
    client = SupabaseWorkerClient(settings, http_client)
    provider = GroqProvider(settings, http_client)
    next_retention_scan = 0.0
    prefer_cleanup = True
    while True:
        try:
            now = time.monotonic()
            if now >= next_retention_scan:
                due_count = await client.enqueue_expired_resumes()
                if due_count:
                    logger.info("Expired resumes queued for private cleanup", extra={"count": due_count})
                next_retention_scan = now + 60

            did_work = False
            if prefer_cleanup:
                cleanup = await client.claim_storage_cleanup(worker_id)
                if cleanup is not None:
                    await process_cleanup_job(cleanup, worker_id, client)
                    did_work = True
            screening = await client.claim(worker_id)
            if screening is not None:
                await process_claimed_job(screening, worker_id, settings, client, provider)
                did_work = True
            if not prefer_cleanup:
                cleanup = await client.claim_storage_cleanup(worker_id)
                if cleanup is not None:
                    await process_cleanup_job(cleanup, worker_id, client)
                    did_work = True
            prefer_cleanup = not prefer_cleanup
            if not did_work:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            logger.warning("Screening queue unavailable", extra={"service_code": exc.code})
            await asyncio.sleep(5)
