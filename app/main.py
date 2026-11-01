"""FastAPI application: submission, status polling, health and readiness.

Long-running screening never runs here; handlers only validate, persist, and return.
Run with:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import Settings, get_settings
from app.database import (
    INPUT_JD_PDF,
    INPUT_JD_TEXT,
    INPUT_RESUME_PDF,
    Database,
    DatabaseSettings,
    JobRepository,
    NewInput,
)
from app.exceptions import (
    AppError,
    LengthRequired,
    NotFound,
    PayloadTooLarge,
    RateLimited,
    ServiceUnavailable,
    UnsupportedMediaType,
    ValidationFailed,
)
from app.logger import (
    bind_request_id,
    configure_logging,
    get_logger,
    reset_request_id,
)
from app.schemas import (
    ErrorBody,
    ErrorResponse,
    JobErrorInfo,
    JobStatus,
    ScreeningResult,
    StatusResponse,
    SubmissionResponse,
)
from app.security import (
    RateLimiter,
    extract_bearer_token,
    generate_access_token,
    hash_token,
    resolve_client_ip,
    verify_token,
)
from app.utils import sanitize_filename, sha256_hex, utcnow

log = get_logger("app.main")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
_READ_CHUNK = 1024 * 1024
# PDF spec allows the header within the first 1024 bytes. Deep validation lives in resume_parser.py.
_PDF_MAGIC = b"%PDF-"
# Dummy hash used to keep timing similar when a job id does not exist.
_DUMMY_HASH = "0" * 64


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or "unknown"
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id, details=details)
    )
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status_code, headers=headers)


async def _read_upload_limited(upload: UploadFile, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise PayloadTooLarge(f"{label} exceeds the maximum size of {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_absent_upload(value: object) -> bool:
    """Browsers send an empty, nameless file part for an untouched <input type=file>."""
    return isinstance(value, UploadFile) and not value.filename and not value.size


async def _pdf_input(
    upload: UploadFile, *, input_type: str, position: int, label: str, limit: int
) -> NewInput:
    data = await _read_upload_limited(upload, limit, label)
    if not data:
        raise ValidationFailed(f"{label} is empty.")
    if _PDF_MAGIC not in data[:1024]:
        raise ValidationFailed(f"{label} is not a PDF file.")
    filename = sanitize_filename(upload.filename, f"{input_type}_{position}.pdf")
    if input_type == INPUT_JD_PDF:
        return NewInput.jd_pdf(data, filename)
    return NewInput.resume_pdf(data, filename, f"Resume {position}")


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    db = database or Database(DatabaseSettings(settings.database_url))
    repo = JobRepository(db)
    trusted = settings.trusted_proxy_networks
    submit_limiter = RateLimiter(settings.submit_rate_limit_per_minute)
    status_limiter = RateLimiter(settings.status_rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(db.open)
        log.info("service started", extra={"ctx": {"version": __version__}})
        try:
            yield
        finally:
            await run_in_threadpool(db.close)
            log.info("service stopped")

    docs = settings.enable_docs
    app = FastAPI(
        title="Resume-Parser",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.repository = repo

    if settings.origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins_list,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        ctx_token = bind_request_id(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            path = request.url.path
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            if settings.environment == "production":
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            if not (docs and path in _DOC_PATHS):
                response.headers["Content-Security-Policy"] = _CSP
            if path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            level = 10 if request.url.path in ("/health", "/ready") else 20
            log.log(
                level,
                "request completed",
                extra={
                    "ctx": {
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "duration_ms": elapsed_ms,
                    }
                },
            )
            reset_request_id(ctx_token)

    # ---- exception handlers -------------------------------------------------
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.headers, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values; report only locations and error types.
        details = [
            {"loc": [str(p) for p in e.get("loc", ())], "type": str(e.get("type", "invalid"))}
            for e in exc.errors()
        ]
        return _error_response(request, 422, "validation_error", "The request is invalid.", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        message = {404: "Resource not found.", 405: "Method not allowed."}.get(exc.status_code, "Request failed.")
        return _error_response(request, exc.status_code, code, message, dict(exc.headers or {}) or None)

    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        log.error("database error", extra={"ctx": {"exc_type": type(exc).__name__}})
        return _error_response(
            request, 503, "database_unavailable", "The database is temporarily unavailable.",
            {"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"ctx": {"exc_type": type(exc).__name__}})
        return _error_response(request, 500, "internal_error", "An unexpected error occurred.")

    # ---- helpers --------------------------------------------------------------
    def enforce_rate_limit(limiter: RateLimiter, request: Request) -> None:
        allowed, retry_after = limiter.check(resolve_client_ip(request, trusted))
        if not allowed:
            raise RateLimited(headers={"Retry-After": str(retry_after)})

    # ---- endpoints -------------------------------------------------------------
    @app.post("/api/screenings", status_code=202, response_model=SubmissionResponse)
    async def submit_screening(request: Request) -> SubmissionResponse:
        enforce_rate_limit(submit_limiter, request)

        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise UnsupportedMediaType("Content-Type must be multipart/form-data.")
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            raise LengthRequired()
        try:
            length = int(raw_length)
        except ValueError:
            raise ValidationFailed("Invalid Content-Length header.") from None
        if length < 0:
            raise ValidationFailed("Invalid Content-Length header.")
        if length > settings.max_request_bytes:
            raise PayloadTooLarge(f"Request exceeds the maximum size of {settings.max_request_bytes} bytes.")

        form = await request.form()
        try:
            jd_text_field = form.get("job_description_text")
            jd_pdf_field = form.get("job_description_pdf")
            resume_fields = form.getlist("resumes")

            if isinstance(jd_text_field, UploadFile):
                raise ValidationFailed("job_description_text must be text, not a file.")
            if jd_pdf_field is not None and not isinstance(jd_pdf_field, UploadFile):
                raise ValidationFailed("job_description_pdf must be a file.")

            jd_text = (jd_text_field or "").replace("\x00", "").strip()
            jd_pdf = None if jd_pdf_field is None or _is_absent_upload(jd_pdf_field) else jd_pdf_field

            if jd_text and jd_pdf is not None:
                raise ValidationFailed("Provide exactly one job description source: pasted text or a PDF, not both.")
            if not jd_text and jd_pdf is None:
                raise ValidationFailed("Provide exactly one job description source: pasted text or a PDF.")
            if len(jd_text) > settings.max_jd_chars:
                raise PayloadTooLarge(f"Job description text exceeds {settings.max_jd_chars} characters.")

            resumes: list[UploadFile] = []
            for field in resume_fields:
                if _is_absent_upload(field):
                    continue
                if not isinstance(field, UploadFile):
                    raise ValidationFailed("resumes must be PDF files.")
                resumes.append(field)
            if not resumes:
                raise ValidationFailed("At least one resume PDF is required.")
            if len(resumes) > settings.max_resumes_per_job:
                raise ValidationFailed(f"At most {settings.max_resumes_per_job} resumes are allowed per job.")

            inputs: list[NewInput] = []
            if jd_pdf is not None:
                inputs.append(
                    await _pdf_input(
                        jd_pdf, input_type=INPUT_JD_PDF, position=0,
                        label="Job description PDF", limit=settings.max_upload_bytes,
                    )
                )
            else:
                encoded = jd_text.encode("utf-8")
                inputs.append(NewInput.jd_text(jd_text))
            for index, upload in enumerate(resumes, start=1):
                inputs.append(
                    await _pdf_input(
                        upload, input_type=INPUT_RESUME_PDF, position=index,
                        label=f"Resume {index}", limit=settings.max_upload_bytes,
                    )
                )
        finally:
            await form.close()

        token = generate_access_token()
        created = await run_in_threadpool(
            repo.create_job, inputs=inputs,
            token_hash=bytes.fromhex(hash_token(token, settings.pepper_value)),
            retention=timedelta(days=settings.retention_days),
            token_ttl=timedelta(hours=settings.access_token_ttl_hours),
            max_attempts=settings.job_max_attempts,
            correlation_id=getattr(request.state, "request_id", None),
        )
        job_id = created.job_id
        log.info(
            "screening queued",
            extra={"ctx": {"job_id": str(job_id), "resume_count": len(resumes)}},
        )
        return SubmissionResponse(
            job_id=job_id,
            status=JobStatus.QUEUED,
            access_token=token,
            status_endpoint=f"/api/status/{job_id}",
            poll_after_seconds=settings.poll_after_seconds,
        )

    @app.get("/api/status/{job_id}", response_model=StatusResponse, response_model_exclude_none=True)
    async def get_status(
        job_id: UUID, request: Request, authorization: str | None = Header(default=None)
    ) -> StatusResponse:
        enforce_rate_limit(status_limiter, request)
        token = extract_bearer_token(authorization)
        record = await run_in_threadpool(repo.get_job, job_id)

        # Unknown job, wrong token, revoked and expired tokens all yield the same 404
        # so job ids cannot be enumerated.
        presented_hash = bytes.fromhex(hash_token(token, settings.pepper_value))
        token_ok = await run_in_threadpool(repo.verify_token, job_id, presented_hash)
        if record is None or not token_ok:
            raise NotFound("Job not found.")

        status = JobStatus(record.status.value)
        results = None
        error = None
        if status is JobStatus.COMPLETED:
            try:
                results = ScreeningResult.model_validate(record.result).results
            except Exception:  # stored result violates the schema: fail closed
                log.error("stored result failed validation", extra={"ctx": {"job_id": str(job_id)}})
                raise AppError("The result is unavailable.", code="result_unavailable") from None
        elif status is JobStatus.FAILED:
            error = JobErrorInfo(
                code=record.error_code or "processing_failed",
                message=record.error_message or "The screening could not be completed.",
            )
        return StatusResponse(
            job_id=record.id,
            status=status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
            poll_after_seconds=None if status.is_terminal else settings.poll_after_seconds,
            results=results,
            error=error,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness only: no dependency checks, no internal details."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """Readiness: database reachable and schema present."""
        try:
            if not await run_in_threadpool(db.ping):
                raise ServiceUnavailable("Database is unavailable.", code="not_ready")
        except ServiceUnavailable:
            raise
        except Exception:
            raise ServiceUnavailable("Service is not ready.", code="not_ready") from None
        return {"status": "ready"}

    static_dir = Path(settings.static_dir)
    index_file = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        if not index_file.is_file():
            raise NotFound()
        return FileResponse(index_file)

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app


_app_cache: dict[str, FastAPI] = {}


def __getattr__(name: str) -> FastAPI:
    """Lazily build `app` so `uvicorn app.main:app` works without import-time env access."""
    if name == "app":
        if "app" not in _app_cache:
            _app_cache["app"] = create_app()
        return _app_cache["app"]
    raise AttributeError(name)
