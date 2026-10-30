"""FastAPI application factory and foundation routes."""

from __future__ import annotations

import logging
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from app.api_candidates import router as candidates_router
from app.api_demo import router as demo_router
from app.api_jobs import router as jobs_router
from app.api_organizations import router as organizations_router
from app.api_screening import router as screening_router
from app.config import Settings, get_settings
from app.exceptions import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging_config import configure_logging
from app.rate_limit import SlidingWindowLimiter
from app.schema_screening import HealthResponse
from app.security import new_request_id
from app.worker import worker_loop

BASE_LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the application using validated settings."""

    configured = settings or get_settings()
    configure_logging(configured.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        application.state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            follow_redirects=False,
        )
        worker_task: asyncio.Task[None] | None = None
        if configured.screening_worker_enabled:
            worker_task = asyncio.create_task(
                worker_loop(configured, application.state.http_client),
                name="screening-queue-worker",
            )
            application.state.worker_task = worker_task
        BASE_LOGGER.info("Application startup", extra={"environment": configured.app_env})
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            await application.state.http_client.aclose()
            BASE_LOGGER.info("Application shutdown")

    application = FastAPI(
        title=configured.app_name,
        version="0.1.0",
        debug=configured.debug,
        lifespan=lifespan,
    )
    application.state.settings = configured
    application.state.rate_limiter = SlidingWindowLimiter()
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(ApiError, api_error_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)

    for router in (demo_router, organizations_router, jobs_router, candidates_router, screening_router):
        application.include_router(router, prefix="/api/v1")

    if configured.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=configured.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        )

    @application.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = new_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            is_public_demo = request.url.path == "/api/v1/demo/screenings"
            is_upload = request.url.path.endswith("/screenings") or request.url.path.endswith("/jobs/parse-description")
            request_limit = configured.resume_max_bytes * (6 if is_public_demo else 1) + 256 * 1024
            content_length = request.headers.get("content-length")
            if is_upload and content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = request_limit + 1
                if declared_length > request_limit:
                    response = JSONResponse(
                        status_code=413,
                        content={"error": {
                            "code": "request_too_large",
                            "message": "The upload request exceeds the configured size limit",
                            "details": [],
                            "request_id": request_id,
                        }},
                    )
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception as exc:
            response = await unhandled_exception_handler(request, exc)
        route = request.scope.get("route")
        BASE_LOGGER.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    static_dir = Path(__file__).resolve().parent.parent / "static"
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    @application.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    @application.get("/screening", include_in_schema=False)
    async def screening_page() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    @application.get("/candidate", include_in_schema=False)
    async def candidate_page() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    @application.get("/health", response_model=HealthResponse, tags=["operations"])
    async def health() -> HealthResponse:
        """Return process liveness only; this does not probe Supabase."""

        return HealthResponse(status="ok")

    return application


app = create_app()
