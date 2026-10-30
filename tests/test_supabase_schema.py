"""Unit checks for migration ordering and security declarations.

These static checks do not execute PostgreSQL. Real RLS tests live in
``supabase/tests`` and run with ``supabase test db``.
"""

from pathlib import Path
import unittest
from uuid import UUID

from app.schema_screening import IntakeRpcResponse

ROOT = Path(__file__).resolve().parents[1]
SUPABASE_DIR = ROOT / "supabase"


class SupabaseSchemaTests(unittest.TestCase):
    def test_schema_snapshot_matches_ordered_migrations(self) -> None:
        migrations = sorted((SUPABASE_DIR / "migrations").glob("*.sql"))
        self.assertGreaterEqual(len(migrations), 2)
        names = [path.name for path in migrations]
        self.assertEqual(names, sorted(names))
        expected = "\n\n".join(path.read_text(encoding="utf-8").rstrip() for path in migrations) + "\n"
        self.assertEqual((SUPABASE_DIR / "schema.sql").read_text(encoding="utf-8"), expected)

    def test_each_business_table_has_rls_and_tenant_policies(self) -> None:
        core = (SUPABASE_DIR / "migrations" / "20261030000100_core_schema.sql").read_text(encoding="utf-8")
        policies = (SUPABASE_DIR / "migrations" / "20261030000200_rls_storage_intake.sql").read_text(encoding="utf-8")
        tables = (
            "organizations",
            "organization_memberships",
            "jobs",
            "candidates",
            "resume_documents",
            "screening_results",
            "processing_jobs",
            "audit_events",
        )
        for table in tables:
            self.assertIn(f"alter table public.{table} enable row level security", core.lower())
            self.assertIn(f"on public.{table}", policies.lower())
        self.assertIn("private.has_org_role", policies)

    def test_job_criteria_extend_schema_in_a_forward_migration(self) -> None:
        migration = (SUPABASE_DIR / "migrations" / "20261030000300_job_criteria.sql").read_text(encoding="utf-8").lower()
        self.assertIn("alter table public.jobs", migration)
        self.assertIn("required_skills text[]", migration)
        self.assertIn("preferred_skills text[]", migration)
        self.assertIn("minimum_experience_years", migration)
        self.assertIn("contact_email_format_check", migration)

    def test_worker_queue_and_job_requirement_cache_are_scoped(self) -> None:
        migration = (SUPABASE_DIR / "migrations" / "20261030000400_screening_pipeline.sql").read_text(encoding="utf-8").lower()
        self.assertIn("create or replace function public.claim_screening_job", migration)
        self.assertIn("for update skip locked", migration)
        self.assertIn("create or replace function public.complete_screening_job", migration)
        self.assertIn("p_classification_score not between 0 and 100", migration)
        self.assertIn("grant execute on function public.claim_screening_job(text, integer) to service_role", migration)
        self.assertIn("invalidate_ai_job_requirements", migration)
        self.assertIn("set search_path = ''", migration)

    def test_job_criteria_extend_schema_in_a_forward_migration(self) -> None:
        migration = (SUPABASE_DIR / "migrations" / "20261030000300_job_criteria.sql").read_text(encoding="utf-8").lower()
        self.assertIn("alter table public.jobs", migration)
        self.assertIn("required_skills text[]", migration)
        self.assertIn("preferred_skills text[]", migration)
        self.assertIn("minimum_experience_years", migration)
        self.assertIn("contact_email_format_check", migration)

    def test_private_storage_guards_and_safe_definer_search_paths(self) -> None:
        policies = (SUPABASE_DIR / "migrations" / "20261030000200_rls_storage_intake.sql").read_text(encoding="utf-8").lower()
        self.assertIn("values ('private-resumes', 'private-resumes', false", policies)
        self.assertIn("as restrictive for select to anon", policies)
        self.assertIn("as restrictive for select to authenticated", policies)
        self.assertNotIn("create policy public_read", policies)
        self.assertNotIn("grant all on table public", policies)
        self.assertNotIn("set search_path = public", policies)

    def test_rpc_ownership_and_storage_are_tenant_scoped(self) -> None:
        policies = (SUPABASE_DIR / "migrations" / "20261030000200_rls_storage_intake.sql").read_text(encoding="utf-8").lower()
        self.assertIn("auth.uid()", policies)
        self.assertIn("p.organization_id = p_organization_id", policies)
        self.assertIn("private.can_read_resume_path(name)", policies)
        self.assertIn("private.can_upload_resume_path(name)", policies)
        self.assertIn("private.can_delete_resume_path(name)", policies)
        self.assertIn("revoke all on function public.create_screening_intake", policies)
        self.assertIn("pg_advisory_xact_lock", policies)
        self.assertIn("lease_expires_at = null", policies)

    def test_environment_template_has_no_supabase_secrets(self) -> None:
        values = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        self.assertEqual(values["SUPABASE_URL"], "")
        self.assertEqual(values["SUPABASE_ANON_KEY"], "")
        self.assertEqual(values["SUPABASE_SERVICE_ROLE_KEY"], "")

    def test_intake_rpc_response_matches_sql_column_names(self) -> None:
        response = IntakeRpcResponse.model_validate(
            {
                "screening_id": "10000000-0000-4000-8000-000000000001",
                "candidate_id": "20000000-0000-4000-8000-000000000002",
                "resume_document_id": "30000000-0000-4000-8000-000000000003",
                "processing_job_id": "40000000-0000-4000-8000-000000000004",
                "storage_path": "org/candidate/resume.pdf",
                "resume_status": "awaiting_upload",
                "screening_status": "queued",
                "content_sha256": "a" * 64,
                "file_size_bytes": 100,
                "is_replay": False,
            }
        )
        self.assertEqual(response.status, "queued")
        self.assertIsInstance(response.screening_id, UUID)


if __name__ == "__main__":
    unittest.main()
