"""Database/API contract tests for durable batch processing and recovery."""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import SupabaseApi
from app.dependencies import get_supabase_api
from app.main import create_app
from app.worker import process_cleanup_job

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
USER_ID = "5011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
ORG_ID = "6011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
OTHER_ORG_ID = "7011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
JOB_ID = "8011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
SCREENING_ID = "9011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
RESUME_ID = "a011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"


def settings() -> Settings:
    return Settings(_env_file=None, app_env="test", supabase_url="https://unit-test.supabase.co", supabase_anon_key="test-anon-key")


class QueueMigrationContracts(unittest.TestCase):
    def test_duplicate_submission_is_serialized_and_payload_mismatch_conflicts(self) -> None:
        core = (MIGRATIONS / "20261030000100_core_schema.sql").read_text(encoding="utf-8").lower()
        intake = (MIGRATIONS / "20261030000200_rls_storage_intake.sql").read_text(encoding="utf-8").lower()
        self.assertIn("unique (organization_id, requested_by, idempotency_key)", core)
        self.assertIn("pg_advisory_xact_lock", intake)
        self.assertIn("idempotency key payload differs", intake)
        self.assertIn("unique (job_id, resume_document_id)", core)

    def test_concurrent_claims_and_worker_restart_are_lease_fenced(self) -> None:
        pipeline = (MIGRATIONS / "20261030000400_screening_pipeline.sql").read_text(encoding="utf-8").lower()
        queue = (MIGRATIONS / "20261030000500_reliable_workflows.sql").read_text(encoding="utf-8").lower()
        self.assertIn("for update skip locked", queue)
        self.assertIn("lease_expires_at <= now()", queue)
        self.assertIn("worker_id = p_worker_id", queue)
        self.assertIn("lease_expires_at > now()", queue)
        self.assertIn("q.lease_expires_at > now()", pipeline)
        self.assertIn("eligible_r.status = 'available'", queue)
        self.assertIn("q.lease_expires_at > now()", queue)

    def test_retries_are_bounded_and_permanent_errors_fail(self) -> None:
        pipeline = (MIGRATIONS / "20261030000400_screening_pipeline.sql").read_text(encoding="utf-8").lower()
        cleanup = (MIGRATIONS / "20261030000500_reliable_workflows.sql").read_text(encoding="utf-8").lower()
        self.assertIn("q.attempt_count < q.max_attempts", pipeline)
        self.assertIn("case when p_retryable and q.attempt_count < q.max_attempts then 'retryable_failure' else 'failed' end", pipeline)
        self.assertIn("attempt_count integer not null default 0", cleanup)
        self.assertIn("max_attempts integer not null default 8", cleanup)
        self.assertIn("power(2::numeric", cleanup)

    def test_status_updates_and_retention_cleanup_are_durable(self) -> None:
        queue = (MIGRATIONS / "20261030000500_reliable_workflows.sql").read_text(encoding="utf-8").lower()
        self.assertIn("storage_cleanup_jobs", queue)
        self.assertIn("enqueue_expired_resume_cleanup", queue)
        self.assertIn("interval '365 days'", queue)
        self.assertIn("interval '60 minutes'", queue)
        self.assertIn("set status = 'failed'", queue)
        self.assertIn("report_json = null", queue)
        self.assertIn("revoke all on function public.delete_resume_document(uuid) from authenticated", queue)

    def test_retry_rpc_requires_retryable_state_and_remaining_attempts(self) -> None:
        queue = (MIGRATIONS / "20261030000500_reliable_workflows.sql").read_text(encoding="utf-8").lower()
        self.assertIn("create or replace function public.retry_screening_job", queue)
        self.assertIn("s.status = 'retryable_failure'", queue)
        self.assertIn("q.status = 'retryable_failure'", queue)
        self.assertIn("q.attempt_count < q.max_attempts", queue)
        self.assertIn("grant execute on function public.retry_screening_job(uuid, uuid) to authenticated", queue)

    def test_frontend_preserves_individual_batch_outcomes_and_has_recovery_controls(self) -> None:
        script = (ROOT / "static" / "js" / "screening.js").read_text(encoding="utf-8")
        self.assertIn("invalidReason", script)
        self.assertIn("await uploadOne(file, status, row, idempotencyKeys[index])", script)
        self.assertIn("history-previous", script)
        self.assertIn("history-next", script)
        self.assertIn("retryable_failure", script)
        self.assertIn("Compare selected", (ROOT / "static" / "screening.html").read_text(encoding="utf-8"))


class ApiHistoryTests(unittest.TestCase):
    def _client(self, *, member: bool = True, rows: list[dict] | None = None, rpc_result: str | None = "retryable_failure"):
        def responder(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID, "email": "recruiter@example.test"})
            if request.url.path == "/rest/v1/organization_memberships":
                actual = request.url.params.get("organization_id")
                allowed = actual == f"eq.{ORG_ID}" and member
                return httpx.Response(200, json=[{"role": "recruiter"}] if allowed else [])
            if request.url.path == "/rest/v1/screening_results":
                return httpx.Response(200, json=rows or [])
            if request.url.path == "/rest/v1/rpc/retry_screening_job":
                if rpc_result is None:
                    return httpx.Response(404, json={"code": "P0002", "message": "screening is not retryable"})
                return httpx.Response(200, json=rpc_result)
            return httpx.Response(404, json={"code": "not_found"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        api = SupabaseApi(settings(), upstream)
        application = create_app(settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        return application, upstream

    def test_history_uses_server_pagination_search_and_status(self) -> None:
        rows = [{
            "id": SCREENING_ID, "job_id": JOB_ID, "candidate_id": RESUME_ID,
            "resume_document_id": RESUME_ID, "status": "retryable_failure",
            "job_match_score": None, "classification_score": None,
            "score_version": None, "created_at": "2026-10-30T00:00:00Z",
            "candidate": {"full_name": "Candidate", "contact_email": "candidate@example.test"},
            "job": {"title": "Engineer"},
            "processing": {"last_error_code": "provider_timeout", "attempt_count": 2, "max_attempts": 3},
        }]
        rows.append({**rows[0], "id": "a011b33d-4e0d-4b47-8e3e-8c37b9c8b1f3"})
        application, upstream = self._client(rows=rows)
        try:
            with TestClient(application) as client:
                response = client.get(
                    f"/api/v1/organizations/{ORG_ID}/screenings?limit=1&offset=20&search=Engineer&status=retryable_failure",
                    headers={"Authorization": "Bearer test-user-token"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["offset"], 20)
            self.assertEqual(result["limit"], 1)
            self.assertTrue(result["has_more"])
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["error_code"], "provider_timeout")
            self.assertEqual(result["items"][0]["attempt_count"], 2)
        finally:
            application.dependency_overrides.clear()
            import asyncio
            asyncio.run(upstream.aclose())

    def test_csv_export_requires_tenant_membership_and_escapes_formulas(self) -> None:
        rows = [{
            "id": SCREENING_ID, "job_id": JOB_ID, "candidate_id": RESUME_ID,
            "resume_document_id": RESUME_ID, "status": "completed",
            "job_match_score": 8.5, "classification_score": 85,
            "score_version": "rubric-v1", "created_at": "2026-10-30T00:00:00Z",
            "candidate": {"full_name": "=HYPERLINK(\"bad\")", "contact_email": "+SUM(A1:A2)"},
            "job": {"title": "@role"}, "processing": {},
        }]
        application, upstream = self._client(rows=rows)
        try:
            with TestClient(application) as client:
                allowed = client.get(f"/api/v1/organizations/{ORG_ID}/screenings/export.csv", headers={"Authorization": "Bearer test-user-token"})
                denied = client.get(f"/api/v1/organizations/{OTHER_ORG_ID}/screenings/export.csv", headers={"Authorization": "Bearer test-user-token"})
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertIn("'=HYPERLINK", allowed.text)
            self.assertIn("'+SUM", allowed.text)
            self.assertEqual(denied.status_code, 404)
        finally:
            application.dependency_overrides.clear()
            import asyncio
            asyncio.run(upstream.aclose())

    def test_csv_export_accepts_exactly_five_thousand_rows(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID, "email": "recruiter@example.test"})
            if request.url.path == "/rest/v1/organization_memberships":
                return httpx.Response(200, json=[{"role": "recruiter"}])
            if request.url.path == "/rest/v1/screening_results":
                offset = int(request.url.params.get("offset", "0"))
                limit = int(request.url.params.get("limit", "200"))
                return httpx.Response(200, json=[{
                    "id": str(uuid.UUID(int=offset + index + 1)),
                    "job_id": JOB_ID, "candidate_id": RESUME_ID, "resume_document_id": RESUME_ID,
                    "status": "completed", "job_match_score": 8.0, "classification_score": 80,
                    "score_version": "rubric-v1", "created_at": "2026-10-30T00:00:00Z",
                    "candidate": {"full_name": "Candidate", "contact_email": "candidate@example.test"},
                    "job": {"title": "Engineer"}, "processing": {},
                } for index in range(min(limit, max(0, 5000 - offset)))] )
            return httpx.Response(404, json={"code": "not_found"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        api = SupabaseApi(settings(), upstream)
        application = create_app(settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        try:
            with TestClient(application) as client:
                response = client.get(
                    f"/api/v1/organizations/{ORG_ID}/screenings/export.csv",
                    headers={"Authorization": "Bearer test-user-token"},
                )
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertEqual(len(response.text.splitlines()), 5001)
        finally:
            application.dependency_overrides.clear()
            import asyncio
            asyncio.run(upstream.aclose())

    def test_manual_retry_is_authorized_and_only_returns_accepted_state(self) -> None:
        application, upstream = self._client()
        try:
            with TestClient(application) as client:
                response = client.post(
                    f"/api/v1/organizations/{ORG_ID}/screenings/{SCREENING_ID}/retry",
                    headers={"Authorization": "Bearer test-user-token"},
                )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["status"], "retryable_failure")
        finally:
            application.dependency_overrides.clear()
            import asyncio
            asyncio.run(upstream.aclose())

    def test_retry_race_returns_conflict_instead_of_internal_error(self) -> None:
        application, upstream = self._client(rpc_result=None)
        try:
            with TestClient(application) as client:
                response = client.post(
                    f"/api/v1/organizations/{ORG_ID}/screenings/{SCREENING_ID}/retry",
                    headers={"Authorization": "Bearer test-user-token"},
                )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "screening_not_retryable")
        finally:
            application.dependency_overrides.clear()
            import asyncio
            asyncio.run(upstream.aclose())


class CleanupWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_404_is_idempotent_and_completes_lease(self) -> None:
        from unittest.mock import AsyncMock

        client = AsyncMock()
        await process_cleanup_job(
            {"cleanup_job_id": "cleanup-1", "storage_path": "org/candidate/resume.pdf"},
            "worker-1", client,
        )
        client.delete_private_resume.assert_awaited_once_with("org/candidate/resume.pdf")
        client.complete_storage_cleanup.assert_awaited_once_with("cleanup-1", "worker-1")
        client.fail_storage_cleanup.assert_not_awaited()

    async def test_transient_cleanup_failure_is_persisted_as_retryable(self) -> None:
        from unittest.mock import AsyncMock
        from app.exceptions import ApiError

        client = AsyncMock()
        client.delete_private_resume.side_effect = ApiError(503, "storage_cleanup_unavailable", "safe")
        client.fail_storage_cleanup.return_value = "retryable_failure"
        await process_cleanup_job(
            {"cleanup_job_id": "cleanup-2", "storage_path": "org/candidate/resume.pdf"},
            "worker-1", client,
        )
        values = client.fail_storage_cleanup.await_args.args
        self.assertEqual(values, ("cleanup-2", "worker-1", "storage_cleanup_unavailable", True))


if __name__ == "__main__":
    unittest.main()
