"""Stateless, AI-assisted public screening endpoint for the dashboard."""

from __future__ import annotations

import hashlib
import logging
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import ValidationError

from app.ai import AIProviderError, GroqProvider
from app.config import Settings
from app.dependencies import SettingsDependency
from app.exceptions import ApiError
from app.integrity import analyze_integrity
from app.job_description import extract_job_description_pdf
from app.links import extract_profile_links
from app.rate_limit import enforce_request_limit
from app.resume_parser import DocumentProcessingError, extract_pdf
from app.schema_demo import DemoCandidateResult, DemoScreeningResponse
from app.scoring import score_candidate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public demo"])
MAX_DEMO_FILES = 5
MAX_JOB_DESCRIPTION_CHARS = 100_000
DEMO_OCR_MAX_PAGES = 1
DEMO_OCR_TIMEOUT_SECONDS = 5


def _safe_filename(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/")
    name = PurePosixPath(normalized).name
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or not name.lower().endswith(".pdf")
        or any(ord(char) < 32 for char in name)
    ):
        raise ApiError(400, "invalid_resume", "Choose a valid PDF resume")
    return name


def _safe_job_description(text: str | None) -> str:
    description = (text or "").strip()
    if not description:
        raise ApiError(422, "job_description_required", "Paste a job description or upload a PDF.")
    if len(description) > MAX_JOB_DESCRIPTION_CHARS:
        raise ApiError(413, "job_description_too_long", "The job description exceeds 100,000 characters.")
    return description


@router.post("/demo/screenings", response_model=DemoScreeningResponse)
async def create_demo_screenings(
    request: Request,
    settings: SettingsDependency,
    resumes: Annotated[list[UploadFile], File()],
    privacy_acknowledged: Annotated[bool, Form()],
    job_description: Annotated[str | None, Form(max_length=MAX_JOB_DESCRIPTION_CHARS)] = None,
    job_description_pdf: Annotated[UploadFile | None, File()] = None,
) -> DemoScreeningResponse:
    """Analyze PDFs in the request and return results without persisting them."""

    if not 1 <= len(resumes) <= MAX_DEMO_FILES:
        raise ApiError(422, "resume_count_invalid", f"Upload between 1 and {MAX_DEMO_FILES} PDF resumes.")
    if not privacy_acknowledged:
        raise ApiError(422, "privacy_confirmation_required", "Confirm you are authorized to process these resumes.")
    remote_host = request.client.host if request.client else "unknown"
    remote_key = hashlib.sha256(remote_host.encode("utf-8", errors="replace")).hexdigest()
    enforce_request_limit(request, f"public-demo:{remote_key}", limit=5, window_seconds=3600)

    try:
        if job_description_pdf is not None:
            if job_description_pdf.content_type not in {"application/pdf", "application/octet-stream"}:
                raise ApiError(400, "invalid_job_description_pdf", "Choose a PDF job description")
            if not (job_description_pdf.filename or "").lower().endswith(".pdf"):
                raise ApiError(400, "invalid_job_description_pdf", "Choose a PDF job description")
            job_pdf = await job_description_pdf.read(settings.resume_max_bytes + 1)
            if not job_pdf:
                raise ApiError(400, "empty_job_description_pdf", "The uploaded job-description PDF is empty.")
            if len(job_pdf) > settings.resume_max_bytes:
                raise ApiError(413, "job_description_too_large", "The job-description PDF exceeds the 10 MiB limit.")
            description = extract_job_description_pdf(job_pdf, max_pages=settings.pdf_max_pages)
        else:
            description = _safe_job_description(job_description)

        provider = GroqProvider(settings, request.app.state.http_client)
        try:
            requirements = await provider.extract_job_requirements(
                title="Candidate screening role",
                description=description,
                required_skills=[],
                preferred_skills=[],
                experience_years=None,
                qualifications="",
                responsibilities="",
            )
        except AIProviderError as exc:
            raise ApiError(503, exc.code, exc.safe_message) from None
        if not any((requirements.required_skills, requirements.experience_expectations,
                    requirements.responsibilities, requirements.qualifications)):
            raise ApiError(422, "job_requirements_missing", "The job description did not provide clear screening criteria. Add explicit skills, responsibilities, or qualifications.")

        results: list[DemoCandidateResult] = []
        for upload in resumes:
            file_name = (upload.filename or "resume.pdf")[:255]
            processing_stage = "resume_validation"
            try:
                safe_name = _safe_filename(upload.filename)
                if upload.content_type not in {"application/pdf", "application/octet-stream"}:
                    raise DocumentProcessingError("invalid_pdf_mime_type", "Upload a PDF resume.")
                content = await upload.read(settings.resume_max_bytes + 1)
                if not content:
                    raise DocumentProcessingError("empty_pdf", "The uploaded PDF is empty.")
                if len(content) > settings.resume_max_bytes:
                    raise DocumentProcessingError("pdf_too_large", "The resume exceeds the 10 MiB limit.")
                demo_settings = settings.model_copy(update={
                    "ocr_max_pages": min(settings.ocr_max_pages, DEMO_OCR_MAX_PAGES),
                    "ocr_timeout_seconds": min(settings.ocr_timeout_seconds, DEMO_OCR_TIMEOUT_SECONDS),
                })
                processing_stage = "pdf_extraction"
                document = extract_pdf(content, demo_settings, mime_type=upload.content_type or "application/pdf")
                processing_stage = "candidate_extraction"
                analysis = await provider.extract_candidate(document, requirements)
                processing_stage = "report_scoring"
                report = score_candidate(
                    requirements,
                    analysis,
                    document,
                    extract_profile_links(document.text),
                    analyze_integrity(document),
                    settings,
                    hashlib.sha256(content).hexdigest(),
                )
                matched_skills = [
                    item.requirement for item in report.profile.assessments
                    if item.category == "required_skills" and item.status in {"supported", "partial"}
                ]
                missing_requirements = [
                    item.requirement for item in report.profile.assessments
                    if item.status in {"missing", "uncertain"}
                ]
                results.append(DemoCandidateResult(
                    file_name=safe_name,
                    status="completed",
                    message="Screening completed. Review the evidence and provisional scores.",
                    report=report,
                    matched_skills=matched_skills,
                    missing_requirements=missing_requirements,
                ))
            except (DocumentProcessingError, ApiError, AIProviderError, ValidationError, ValueError) as exc:
                validation_summary = ""
                if isinstance(exc, ValidationError):
                    validation_summary = "; ".join(
                        ".".join(str(part) for part in error["loc"]) + ":" + error["type"]
                        for error in exc.errors(include_input=False)
                    )
                logger.warning(
                    "Candidate processing did not produce a report at %s (%s)",
                    processing_stage,
                    validation_summary or type(exc).__name__,
                    extra={"processing_stage": processing_stage, "exception_type": type(exc).__name__},
                )
                safe_message = getattr(exc, "safe_message", None) or getattr(exc, "message", None)
                if isinstance(exc, ValidationError) and processing_stage == "report_scoring":
                    safe_message = "The generated report failed validation. The screening was not completed; retry the request."
                if not safe_message:
                    safe_message = "This resume could not be analyzed. Check the PDF and try again."
                results.append(DemoCandidateResult(
                    file_name=file_name,
                    status="failed",
                    message=str(safe_message)[:500],
                    report=None,
                ))
            except Exception as exc:
                logger.error("Unexpected public demo candidate processing error", extra={"exception_type": type(exc).__name__})
                raise
        return DemoScreeningResponse(results=results)
    finally:
        for upload in resumes:
            await upload.close()
        if job_description_pdf is not None:
            await job_description_pdf.close()

