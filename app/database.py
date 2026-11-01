"""PostgreSQL persistence and durable job queue (strict Render-only MVP).

* One synchronous psycopg 3 connection pool (``Database``).
* ``JobRepository``: job creation, token verification, queue operations,
  retention cleanup. All SQL is parameterised; every write runs in an explicit
  transaction.
* Queue semantics live in PostgreSQL functions (see
  db/migrations/20261031_02_queue_functions.sql); this module only calls them.

FastAPI handlers must call these blocking methods from ``def`` endpoints (run in
the threadpool) or via ``run_in_threadpool``; the worker calls them directly.
Never log ``DATABASE_URL``, token hashes, resume bytes or result contents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger("resume_parser.database")

INPUT_JD_TEXT = "jd_text"
INPUT_JD_PDF = "jd_pdf"
INPUT_RESUME_PDF = "resume_pdf"

MAX_INPUT_BYTES_HARD = 50 * 1024 * 1024  # mirrors the DB CHECK backstop
MAX_RESULT_BYTES = 5 * 1024 * 1024
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_.]{1,64}$")


# --------------------------------------------------------------------------
# Errors and enums
# --------------------------------------------------------------------------
class DatabaseError(RuntimeError):
    """Unexpected database condition (message is safe to log)."""


class InvalidJobInput(ValueError):
    """The caller supplied an invalid job definition."""


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class InputType(StrEnum):
    JD_TEXT = "jd_text"
    JD_PDF = "jd_pdf"
    RESUME_PDF = "resume_pdf"


class CompleteOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"  # idempotent repeat by the same attempt
    STALE = "stale"  # caller lost the lease / a newer attempt owns the job
    MISSING = "missing"


class FailOutcome(StrEnum):
    REQUEUED = "requeued"
    FAILED = "failed"
    ALREADY_FAILED = "already_failed"
    STALE = "stale"
    MISSING = "missing"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DatabaseSettings:
    """Connection settings. ``database_url`` is excluded from repr()."""

    database_url: str = field(repr=False)
    pool_min_size: int = 1
    pool_max_size: int = 5
    pool_timeout_seconds: float = 10.0
    connect_timeout_seconds: int = 10
    statement_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a postgresql:// or postgres:// URL")
        if not 0 <= self.pool_min_size <= self.pool_max_size or self.pool_max_size < 1:
            raise ValueError("invalid pool sizes")
        if self.statement_timeout_ms < 1000:
            raise ValueError("statement_timeout_ms must be >= 1000")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DatabaseSettings:
        source = os.environ if env is None else env
        url = source.get("DATABASE_URL", "").strip()
        if not url:
            raise ValueError("DATABASE_URL is required")
        return cls(
            database_url=url,
            pool_min_size=int(source.get("DB_POOL_MIN", "1")),
            pool_max_size=int(source.get("DB_POOL_MAX", "5")),
            pool_timeout_seconds=float(source.get("DB_POOL_TIMEOUT_SECONDS", "10")),
            statement_timeout_ms=int(source.get("DB_STATEMENT_TIMEOUT_MS", "30000")),
        )


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
def _safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    cleaned = cleaned.strip().strip(".")[:255]
    return cleaned or None


@dataclass(frozen=True)
class NewInput:
    """One input to persist with a new job. Positions are assigned by the repo."""

    input_type: InputType
    content: bytes | None = field(default=None, repr=False)
    text_content: str | None = field(default=None, repr=False)
    filename: str | None = None
    content_type: str | None = None
    candidate_label: str | None = None

    @classmethod
    def jd_text(cls, text: str) -> NewInput:
        return cls(InputType.JD_TEXT, text_content=text)

    @classmethod
    def jd_pdf(cls, data: bytes, filename: str | None = None) -> NewInput:
        return cls(InputType.JD_PDF, content=data, filename=filename,
                   content_type="application/pdf")

    @classmethod
    def resume_pdf(
        cls, data: bytes, filename: str | None = None, candidate_label: str | None = None
    ) -> NewInput:
        return cls(InputType.RESUME_PDF, content=data, filename=filename,
                   content_type="application/pdf", candidate_label=candidate_label)


@dataclass(frozen=True)
class CreatedJob:
    job_id: uuid.UUID
    token_expires_at: datetime


@dataclass(frozen=True)
class JobRecord:
    id: uuid.UUID
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    finished_by: str | None
    finished_attempt: int | None
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None = field(repr=False)
    retention_expires_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JobRecord:
        data = dict(row)
        data["status"] = JobStatus(data["status"])
        return cls(**{name: data[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class JobInputRecord:
    id: uuid.UUID
    job_id: uuid.UUID
    input_type: InputType
    position: int
    candidate_label: str | None
    filename: str | None
    content_type: str | None
    size_bytes: int
    sha256: str
    content: bytes | None = field(repr=False)
    text_content: str | None = field(repr=False)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JobInputRecord:
        data = dict(row)
        data["input_type"] = InputType(data["input_type"])
        if data["content"] is not None:
            data["content"] = bytes(data["content"])
        data["sha256"] = str(data["sha256"]).strip()
        return cls(**{name: data[name] for name in cls.__dataclass_fields__})


_JOB_COLUMNS = (
    "id, status, created_at, updated_at, started_at, completed_at, attempts, "
    "max_attempts, next_attempt_at, lease_owner, lease_expires_at, finished_by, "
    "finished_attempt, error_code, error_message, result, retention_expires_at"
)
_INPUT_COLUMNS = (
    "id, job_id, input_type, position, candidate_label, filename, content_type, "
    "size_bytes, sha256, content, text_content"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Connection pool
# --------------------------------------------------------------------------
class Database:
    """Owns the psycopg connection pool."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        options = (
            f"-c statement_timeout={settings.statement_timeout_ms} "
            "-c idle_in_transaction_session_timeout=60000 -c timezone=UTC"
        )
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout_seconds,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": settings.connect_timeout_seconds,
                "application_name": "resume-parser",
                "options": options,
            },
            open=False,
            name="resume-parser",
        )

    def open(self) -> None:
        self._pool.open(wait=True, timeout=self._settings.pool_timeout_seconds)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Database:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """Yield a connection inside an explicit transaction (commit/rollback)."""
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    def ping(self) -> bool:
        """Cheap readiness probe; never raises."""
        try:
            with self.transaction() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - readiness must not raise
            logger.warning("database ping failed")
            return False

    def applied_migrations(self) -> list[str]:
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present"
            ).fetchone()
            if not exists or not exists["present"]:
                return []
            rows = conn.execute(
                "SELECT version FROM public.schema_migrations ORDER BY version"
            ).fetchall()
            return [r["version"] for r in rows]


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------
def _dumps_strict(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class JobRepository:
    """All job persistence and queue operations."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---- creation --------------------------------------------------------
    def create_job(
        self,
        *,
        inputs: Sequence[NewInput],
        token_hash: bytes,
        retention: timedelta,
        token_ttl: timedelta,
        max_attempts: int = 3,
        correlation_id: str | None = None,
    ) -> CreatedJob:
        """Persist a queued job, its inputs and a hashed access token atomically."""
        token_hash = bytes(token_hash)
        if len(token_hash) != 32:
            raise InvalidJobInput("token_hash must be a 32-byte SHA-256 digest")
        if retention <= timedelta(0) or token_ttl <= timedelta(0):
            raise InvalidJobInput("retention and token_ttl must be positive")
        if not 1 <= max_attempts <= 20:
            raise InvalidJobInput("max_attempts must be between 1 and 20")
        rows = self._prepare_inputs(inputs)

        job_id = uuid.uuid4()
        now = utcnow()
        token_expires_at = now + token_ttl
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO public.screening_jobs (id, max_attempts, retention_expires_at) "
                "VALUES (%s, %s, %s)",
                (job_id, max_attempts, now + retention),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO public.screening_inputs "
                    "(job_id, input_type, position, candidate_label, filename, content_type, "
                    " size_bytes, sha256, content, text_content) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(job_id, *row) for row in rows],
                )
            conn.execute(
                "INSERT INTO public.job_access_tokens (job_id, token_hash, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (job_id, token_hash, now, token_expires_at),
            )
            self._insert_event(
                conn, "job.created", job_id, correlation_id,
                {"resume_count": sum(1 for r in rows if r[0] == InputType.RESUME_PDF.value)},
            )
        return CreatedJob(job_id=job_id, token_expires_at=token_expires_at)

    @staticmethod
    def _prepare_inputs(inputs: Sequence[NewInput]) -> list[tuple[Any, ...]]:
        jd = [i for i in inputs if i.input_type in (InputType.JD_TEXT, InputType.JD_PDF)]
        resumes = [i for i in inputs if i.input_type == InputType.RESUME_PDF]
        if len(jd) != 1:
            raise InvalidJobInput("exactly one job description (text or PDF) is required")
        if not resumes:
            raise InvalidJobInput("at least one resume PDF is required")

        rows: list[tuple[Any, ...]] = []
        ordered = [(jd[0], 0)] + [(r, idx) for idx, r in enumerate(resumes)]
        for item, position in ordered:
            if item.input_type == InputType.JD_TEXT:
                text = item.text_content
                if text is None or not text.strip():
                    raise InvalidJobInput("job description text is empty")
                if "\x00" in text:
                    raise InvalidJobInput("job description text contains NUL characters")
                payload = text.encode("utf-8")
                content, text_content = None, text
            else:
                data = item.content
                if not data:
                    raise InvalidJobInput("PDF input is empty")
                payload = bytes(data)
                content, text_content = payload, None
            if len(payload) > MAX_INPUT_BYTES_HARD:
                raise InvalidJobInput("input exceeds the hard storage limit")
            label = (item.candidate_label or "").strip() or None
            if label is not None and len(label) > 100:
                raise InvalidJobInput("candidate label is too long")
            rows.append((
                item.input_type.value, position, label, _safe_filename(item.filename),
                item.content_type, len(payload), hashlib.sha256(payload).hexdigest(),
                content, text_content,
            ))
        return rows

    # ---- reads -----------------------------------------------------------
    def get_job(self, job_id: uuid.UUID) -> JobRecord | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM public.screening_jobs WHERE id = %s", (job_id,)
            ).fetchone()
        return JobRecord.from_row(row) if row else None

    def get_inputs(self, job_id: uuid.UUID) -> list[JobInputRecord]:
        """Return all inputs (JD first, then resumes by position). Loads PDF bytes."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_INPUT_COLUMNS} FROM public.screening_inputs WHERE job_id = %s "
                "ORDER BY (input_type = 'resume_pdf'), position",
                (job_id,),
            ).fetchall()
        return [JobInputRecord.from_row(r) for r in rows]

    # ---- tokens ----------------------------------------------------------
    def verify_token(self, job_id: uuid.UUID, token_hash: bytes) -> bool:
        """True iff a non-revoked, unexpired token with this hash exists for the job.

        Lookup is by the SHA-256 of a high-entropy token, so equality search
        leaks nothing useful through timing.
        """
        token_hash = bytes(token_hash)
        if len(token_hash) != 32:
            return False
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT 1 AS ok FROM public.job_access_tokens "
                "WHERE job_id = %s AND token_hash = %s AND revoked_at IS NULL "
                "AND expires_at > clock_timestamp() LIMIT 1",
                (job_id, token_hash),
            ).fetchone()
        return row is not None

    def revoke_tokens(self, job_id: uuid.UUID) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE public.job_access_tokens SET revoked_at = clock_timestamp() "
                "WHERE job_id = %s AND revoked_at IS NULL",
                (job_id,),
            )
            return cur.rowcount

    # ---- queue -----------------------------------------------------------
    def claim_next_job(self, worker_id: str, lease_seconds: int) -> JobRecord | None:
        """Atomically claim one due job, or return None. Reclaims expired leases."""
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM public.claim_next_job(%s, %s)",
                (worker_id, lease_seconds),
            ).fetchone()
        return JobRecord.from_row(row) if row else None

    def heartbeat(self, job_id: uuid.UUID, worker_id: str, attempt: int, lease_seconds: int) -> bool:
        """Extend the lease. False means the worker lost the job and must stop."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT public.heartbeat_job(%s, %s, %s, %s) AS ok",
                (job_id, worker_id, attempt, lease_seconds),
            ).fetchone()
        return bool(row and row["ok"])

    def complete_job(
        self, job_id: uuid.UUID, worker_id: str, attempt: int, result: dict[str, Any]
    ) -> CompleteOutcome:
        """Idempotent, fenced completion. Never overwrites a newer attempt's result."""
        try:
            payload = _dumps_strict(result)
        except (TypeError, ValueError) as exc:
            raise InvalidJobInput("result is not strictly JSON-serialisable") from exc
        if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
            raise InvalidJobInput("result exceeds the maximum stored size")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT public.complete_job(%s, %s, %s, %s::jsonb) AS outcome",
                (job_id, worker_id, attempt, Jsonb(result, dumps=_dumps_strict)),
            ).fetchone()
        return self._parse_outcome(CompleteOutcome, row)

    def fail_job(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        attempt: int,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        backoff_seconds: int = 0,
    ) -> FailOutcome:
        """Fenced failure: re-queue with backoff if retryable and attempts remain."""
        if not _ERROR_CODE_RE.match(error_code):
            raise InvalidJobInput("error_code must match ^[a-z0-9_]{1,64}$")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT public.fail_job(%s, %s, %s, %s, %s, %s, %s) AS outcome",
                (job_id, worker_id, attempt, error_code, error_message[:500],
                 retryable, backoff_seconds),
            ).fetchone()
        return self._parse_outcome(FailOutcome, row)

    @staticmethod
    def _parse_outcome(enum_type: type[StrEnum], row: Mapping[str, Any] | None) -> Any:
        try:
            return enum_type(row["outcome"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError) as exc:
            raise DatabaseError("unexpected queue function result") from exc

    # ---- events ----------------------------------------------------------
    @staticmethod
    def _insert_event(
        conn: psycopg.Connection[dict[str, Any]],
        event_type: str,
        job_id: uuid.UUID | None,
        correlation_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if not _EVENT_TYPE_RE.match(event_type):
            raise InvalidJobInput("invalid audit event type")
        conn.execute(
            "INSERT INTO public.audit_events (job_id, event_type, correlation_id, detail) "
            "VALUES (%s, %s, %s, %s)",
            (job_id, event_type, (correlation_id or None) and correlation_id[:64],
             Jsonb(detail or {}, dumps=_dumps_strict)),
        )

    def record_event(
        self,
        event_type: str,
        *,
        job_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record a minimal audit event. ``detail`` must not contain secrets or PII."""
        with self._db.transaction() as conn:
            self._insert_event(conn, event_type, job_id, correlation_id, detail)

    # ---- retention cleanup ----------------------------------------------
    def find_expired_job_ids(self, limit: int = 100) -> list[uuid.UUID]:
        """Terminal jobs past retention. Active (queued/processing) jobs never match."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM public.screening_jobs "
                "WHERE status IN ('completed', 'failed') AND retention_expires_at <= clock_timestamp() "
                "ORDER BY retention_expires_at LIMIT %s",
                (limit,),
            ).fetchall()
        return [r["id"] for r in rows]

    def delete_expired_jobs(self, *, limit: int = 100, dry_run: bool = True) -> list[uuid.UUID]:
        """Delete (or, by default, only list) expired terminal jobs.

        Cascades to inputs and tokens. Status is re-checked inside the DELETE.
        """
        if dry_run:
            return self.find_expired_job_ids(limit)
        with self._db.transaction() as conn:
            rows = conn.execute(
                "DELETE FROM public.screening_jobs WHERE id IN ("
                "  SELECT id FROM public.screening_jobs "
                "  WHERE status IN ('completed', 'failed') "
                "    AND retention_expires_at <= clock_timestamp() "
                "  ORDER BY retention_expires_at LIMIT %s FOR UPDATE SKIP LOCKED"
                ") AND status IN ('completed', 'failed') RETURNING id",
                (limit,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for jid in ids:
                self._insert_event(conn, "job.purged", None, None, {"job_id": str(jid)})
        return ids

    def delete_old_audit_events(
        self, *, older_than: datetime, limit: int = 1000, dry_run: bool = True
    ) -> int:
        """Delete (or count) audit events older than ``older_than`` (UTC-aware)."""
        if older_than.tzinfo is None:
            raise InvalidJobInput("older_than must be timezone-aware")
        with self._db.transaction() as conn:
            if dry_run:
                row = conn.execute(
                    "SELECT count(*) AS n FROM (SELECT 1 FROM public.audit_events "
                    "WHERE created_at < %s ORDER BY id LIMIT %s) t",
                    (older_than, limit),
                ).fetchone()
                return int(row["n"]) if row else 0
            cur = conn.execute(
                "DELETE FROM public.audit_events WHERE id IN ("
                "  SELECT id FROM public.audit_events WHERE created_at < %s "
                "  ORDER BY id LIMIT %s)",
                (older_than, limit),
            )
            return cur.rowcount
