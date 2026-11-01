import threading

from app.worker import Worker
from app.worker_support import ClaimedJob, StoredInput, WorkerSettings


class FakeRepo:
    def __init__(self):
        self.completed = threading.Event()
        self.claimed = False
    def claim_next_job(self, worker_id, lease_seconds):
        if self.claimed: return None
        self.claimed = True
        return ClaimedJob("job-id", 1, 3)
    def renew_lease(self, *args): return True
    def load_inputs(self, job_id): return [StoredInput("jd_text", 0, text_content="A job description")]
    def complete_job(self, *args): self.completed.set(); return True
    def schedule_retry(self, *args): return True
    def fail_job(self, *args): return True
    def release_job(self, *args): return True


def test_worker_claims_processes_and_completes_one_job():
    repo = FakeRepo()
    settings = WorkerSettings("postgresql://unused", "worker-test", 30, 5, 0.01)
    worker = Worker(repo, settings, runner=lambda jid, inputs, cid: {"results": []})
    assert worker.run_once()
    assert repo.completed.wait(2)
    assert not worker.run_once()
