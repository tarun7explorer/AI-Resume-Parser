"""Worker adapter around the shared psycopg job repository."""
from __future__ import annotations
from uuid import UUID
from app.database import Database, DatabaseSettings, JobRepository, FailOutcome, CompleteOutcome
from app.worker_support import ClaimedJob, StoredInput

class PostgresJobRepository:
    def __init__(self, db): self.db=db; self.repo=JobRepository(db)
    @classmethod
    def from_dsn(cls, dsn):
        db=Database(DatabaseSettings(dsn)); db.open(); return cls(db)
    def close(self): self.db.close()
    def claim_next_job(self, worker_id, lease_seconds):
        job=self.repo.claim_next_job(worker_id,lease_seconds)
        return ClaimedJob(str(job.id),job.attempts,job.max_attempts) if job else None
    def renew_lease(self, job_id, worker_id, attempt, lease_seconds): return self.repo.heartbeat(UUID(job_id),worker_id,attempt,lease_seconds)
    def load_inputs(self, job_id):
        return [StoredInput(x.input_type.value,x.position,x.content,x.text_content,x.filename,x.candidate_label) for x in self.repo.get_inputs(UUID(job_id))]
    def complete_job(self, job_id, worker_id, attempt, result): return self.repo.complete_job(UUID(job_id),worker_id,attempt,result) in (CompleteOutcome.COMPLETED,CompleteOutcome.ALREADY_COMPLETED)
    def schedule_retry(self, job_id, worker_id, attempt, code, message, delay):
        return self.repo.fail_job(UUID(job_id),worker_id,attempt,error_code=code,error_message=message,retryable=True,backoff_seconds=int(delay)) is FailOutcome.REQUEUED
    def fail_job(self, job_id, worker_id, attempt, code, message):
        return self.repo.fail_job(UUID(job_id),worker_id,attempt,error_code=code,error_message=message,retryable=False) in (FailOutcome.FAILED,FailOutcome.ALREADY_FAILED)
    def release_job(self, job_id, worker_id, attempt):
        return self.repo.fail_job(UUID(job_id),worker_id,attempt,error_code="worker_shutdown",error_message="Worker stopped before processing completed.",retryable=True,backoff_seconds=0) is FailOutcome.REQUEUED
