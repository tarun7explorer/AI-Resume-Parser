"""Durable background worker.

Run:  uv run python -m app.worker
Never imported by the web service; the HTTP layer only enqueues jobs.
"""
from __future__ import annotations

import json
import logging
import random
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

from app.worker_support import (
    ClaimedJob, ConfigError, JobRepository, PermanentJobError, StoredInput,
    WorkerSettings, backoff_delay, classify_exception, message_for,
)

log = logging.getLogger("resume_parser.worker")

Runner = Callable[[str, Sequence[StoredInput], str], dict]


def log_event(level: int, event: str, **fields: Any) -> None:
    """Structured key=value logging. Callers must only pass IDs and codes."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    log.log(level, "event=%s %s", event, parts)


class LeaseKeeper(threading.Thread):
    """Renews the lease while a job runs; flags `lost` if ownership is gone."""

    def __init__(self, repo: JobRepository, job: ClaimedJob, settings: WorkerSettings,
                 correlation_id: str) -> None:
        super().__init__(daemon=True, name=f"lease-{job.job_id[:8]}")
        self._repo, self._job, self._s, self._cid = repo, job, settings, correlation_id
        self._stop_evt = threading.Event()
        self.lost = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        last_ok = time.monotonic()
        while not self._stop_evt.wait(self._s.heartbeat_seconds):
            try:
                ok = self._repo.renew_lease(self._job.job_id, self._s.worker_id,
                                            self._job.attempts, self._s.lease_seconds)
            except Exception as exc:
                log_event(logging.WARNING, "lease_renew_error", cid=self._cid,
                          error_type=type(exc).__name__)
                if time.monotonic() - last_ok >= self._s.lease_seconds:
                    self.lost.set()
                    return
                continue
            if not ok:
                log_event(logging.WARNING, "lease_lost", cid=self._cid)
                self.lost.set()
                return
            last_ok = time.monotonic()


class Worker:
    def __init__(self, repo: JobRepository, settings: WorkerSettings,
                 runner: Optional[Runner] = None,
                 rng: Optional[random.Random] = None) -> None:
        self._repo, self._s = repo, settings
        if runner is None:
            from app.screening import run_screening  # lazy: keeps imports light
            runner = run_screening
        self._runner = runner
        self._rng = rng or random.Random()
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    # -- main loop --------------------------------------------------------
    def run_forever(self) -> None:
        log_event(logging.INFO, "worker_started", worker=self._s.worker_id)
        idle = self._s.poll_interval
        error_wait = self._s.poll_interval
        while not self._stop.is_set():
            try:
                processed = self.run_once()
                error_wait = self._s.poll_interval
            except Exception as exc:
                log_event(logging.ERROR, "worker_loop_error", error_type=type(exc).__name__)
                self._stop.wait(error_wait)
                error_wait = min(error_wait * 2, 30.0)
                continue
            if processed:
                idle = self._s.poll_interval
                continue
            self._stop.wait(idle * (0.8 + 0.4 * self._rng.random()))
            idle = min(idle * 2, self._s.poll_max_interval)
        log_event(logging.INFO, "worker_stopped", worker=self._s.worker_id)

    def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was claimed."""
        if self._stop.is_set():
            return False
        job = self._repo.claim_next_job(self._s.worker_id, self._s.lease_seconds)
        if job is None:
            return False
        cid = f"job-{job.job_id}-a{job.attempts}"
        log_event(logging.INFO, "job_claimed", cid=cid, attempt=job.attempts,
                  max_attempts=job.max_attempts)
        if self._stop.is_set():  # stop arrived between poll and claim
            self._release(job, cid)
            return True
        self._process(job, cid)
        return True

    # -- job processing ---------------------------------------------------
    def _process(self, job: ClaimedJob, cid: str) -> None:
        keeper = LeaseKeeper(self._repo, job, self._s, cid)
        keeper.start()
        done = threading.Event()
        holder: dict[str, Any] = {}

        def target() -> None:
            try:
                inputs = self._repo.load_inputs(job.job_id)
                if not inputs:
                    raise PermanentJobError("no_inputs")
                result = self._runner(job.job_id, inputs, cid)
                holder["result"] = self._validate_result(result)
            except Exception as exc:  # classified below, never logged verbatim
                holder["error"] = exc
            finally:
                done.set()

        threading.Thread(target=target, daemon=True, name=f"job-{job.job_id[:8]}").start()

        deadline: Optional[float] = None
        while not done.wait(0.25):
            if self._stop.is_set():
                now = time.monotonic()
                if deadline is None:
                    deadline = now + self._s.shutdown_grace_seconds
                elif now >= deadline:
                    keeper.stop()
                    log_event(logging.WARNING, "shutdown_grace_exceeded", cid=cid)
                    self._release(job, cid)
                    return
        keeper.stop()

        if keeper.lost.is_set():
            log_event(logging.WARNING, "result_discarded_lease_lost", cid=cid)
            return
        try:
            if "error" in holder:
                self._handle_failure(job, holder["error"], cid)
            else:
                ok = self._repo.complete_job(job.job_id, self._s.worker_id,
                                             job.attempts, holder["result"])
                log_event(logging.INFO if ok else logging.WARNING,
                          "job_completed" if ok else "completion_rejected_lease_lost", cid=cid)
        except Exception as exc:
            # Lease will expire and the job will be reclaimed; never crash the loop.
            log_event(logging.ERROR, "finalize_error", cid=cid, error_type=type(exc).__name__)

    def _validate_result(self, result: Any) -> dict:
        if not isinstance(result, dict):
            raise PermanentJobError("invalid_result")
        try:
            encoded = json.dumps(result, allow_nan=False)
        except (TypeError, ValueError):
            raise PermanentJobError("invalid_result") from None
        if len(encoded.encode("utf-8")) > self._s.max_result_bytes:
            raise PermanentJobError("invalid_result")
        return result

    def _handle_failure(self, job: ClaimedJob, exc: BaseException, cid: str) -> None:
        c = classify_exception(exc)
        msg = message_for(c.code)
        if c.transient and job.attempts < job.max_attempts:
            delay = max(
                backoff_delay(job.attempts, self._s.retry_base_seconds,
                              self._s.retry_max_seconds, self._rng),
                min(c.retry_after or 0.0, self._s.retry_max_seconds),
            )
            ok = self._repo.schedule_retry(job.job_id, self._s.worker_id, job.attempts,
                                           c.code, msg, delay)
            log_event(logging.WARNING, "job_retry_scheduled", cid=cid, code=c.code,
                      delay=round(delay, 1), applied=ok)
        else:
            ok = self._repo.fail_job(job.job_id, self._s.worker_id, job.attempts, c.code, msg)
            log_event(logging.ERROR, "job_failed", cid=cid, code=c.code,
                      transient=c.transient, applied=ok)

    def _release(self, job: ClaimedJob, cid: str) -> None:
        try:
            ok = self._repo.release_job(job.job_id, self._s.worker_id, job.attempts)
            log_event(logging.INFO, "job_released", cid=cid, applied=ok)
        except Exception as exc:
            log_event(logging.ERROR, "release_error", cid=cid, error_type=type(exc).__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)sZ %(levelname)s %(name)s %(message)s")
    try:
        settings = WorkerSettings.from_env()
    except ConfigError as exc:
        print(f"worker configuration error: {exc}", file=sys.stderr)
        return 2

    from app.worker_repository import PostgresJobRepository
    repo = PostgresJobRepository.from_dsn(settings.database_url)
    worker = Worker(repo, settings)

    def _handle(signum: int, _frame: Any) -> None:
        log_event(logging.INFO, "signal_received", signal=signum)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        worker.run_forever()
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
