"""Worker configuration and safe failure classification."""
from __future__ import annotations
import math, os, random
from dataclasses import dataclass
from typing import Mapping
from app.config import ConfigError

@dataclass(frozen=True)
class WorkerSettings:
    database_url: str; worker_id: str; lease_seconds: int=180; heartbeat_seconds: int=45
    poll_seconds: float=2.0; retry_base_seconds: float=5; retry_max_seconds: float=300
    shutdown_grace_seconds: int=20; max_result_bytes: int=5*1024*1024
    poll_max_interval: float=30.0
    @property
    def poll_interval(self): return self.poll_seconds
    @classmethod
    def from_env(cls, env: Mapping[str,str]|None=None):
        e=os.environ if env is None else env
        url=e.get("DATABASE_URL","").strip()
        if not url.startswith(("postgresql://","postgres://")): raise ConfigError("DATABASE_URL is required")
        try:
            lease=int(e.get("JOB_LEASE_SECONDS","180"))
            beat=int(e.get("JOB_HEARTBEAT_SECONDS",str(max(1,lease//3))))
            poll=float(e.get("WORKER_POLL_SECONDS","2"))
            retry_base=float(e.get("JOB_RETRY_BASE_SECONDS","5"))
            retry_max=float(e.get("JOB_RETRY_MAX_SECONDS","300"))
            shutdown=int(e.get("WORKER_SHUTDOWN_GRACE_SECONDS","20"))
        except ValueError:
            raise ConfigError("Worker numeric settings must be valid numbers") from None
        if not 10<=lease<=3600 or not 1<=beat<lease: raise ConfigError("Invalid worker lease settings")
        if not (0.1<=poll<=60 and 0<=retry_base<=3600 and retry_base<=retry_max<=86400 and 1<=shutdown<=600):
            raise ConfigError("Worker numeric setting is outside its allowed range")
        worker_id=e.get("WORKER_ID") or f"worker-{os.getpid()}"
        if not worker_id.strip() or len(worker_id)>128:
            raise ConfigError("WORKER_ID is invalid")
        return cls(url,worker_id,lease,beat,poll,retry_base,retry_max,shutdown)

@dataclass(frozen=True)
class StoredInput:
    input_type: str; position: int; data: bytes|None=None; text_content: str|None=None
    filename: str|None=None; candidate_label: str|None=None
@dataclass(frozen=True)
class ClaimedJob:
    job_id: str; attempts: int; max_attempts: int
@dataclass(frozen=True)
class FailureClass:
    code: str; transient: bool=False; retry_after: float|None=None
class PermanentJobError(Exception):
    def __init__(self, code="job_failed"): self.code=code; super().__init__(code)

class JobRepository:
    def claim_next_job(self, worker_id, lease_seconds): raise NotImplementedError
    def renew_lease(self, job_id, worker_id, attempt, lease_seconds): raise NotImplementedError
    def load_inputs(self, job_id): raise NotImplementedError
    def complete_job(self, job_id, worker_id, attempt, result): raise NotImplementedError
    def schedule_retry(self, job_id, worker_id, attempt, code, message, delay): raise NotImplementedError
    def fail_job(self, job_id, worker_id, attempt, code, message): raise NotImplementedError
    def release_job(self, job_id, worker_id, attempt): raise NotImplementedError
def backoff_delay(attempt, base, maximum, rng=None):
    rng=rng or random; return min(maximum,base*(2**max(0,attempt-1)))*(0.5+0.5*rng.random())
def classify_exception(exc):
    from app.ai_provider import AIProviderError
    from app.pipeline_types import ScreeningError
    if isinstance(exc, AIProviderError): return FailureClass(exc.code,exc.retryable,getattr(exc,"retry_after",None))
    if isinstance(exc, ScreeningError): return FailureClass(exc.code,exc.retryable)
    if isinstance(exc, PermanentJobError): return FailureClass(exc.code,False)
    return FailureClass("processing_error",True)
def message_for(code):
    return {"ai_timeout":"The AI provider did not respond in time.","ai_rate_limited":"The AI provider rate limit was reached.","processing_error":"Processing failed temporarily.","invalid_result":"The processor returned an invalid result.","no_inputs":"This job has no stored inputs."}.get(code,"The screening could not be completed.")
