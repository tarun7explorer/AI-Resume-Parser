"""Route contract tests; Supabase HTTP responses are isolated unit fixtures."""

import csv
import io
import unittest
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.api_screening import _csv_cell
from app.config import Settings
from app.database import SupabaseApi
from app.dependencies import get_supabase_api
from app.main import create_app
from app.schema_jobs import JobCreateRequest
from app.job_description import extract_job_description_pdf
from pydantic import ValidationError

USER_ID = "5011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
ORG_ID = "6011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"
JOB_ID = "7011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        supabase_url="https://unit-test.supabase.co",
        supabase_anon_key="public-test-key",
    )


class RecruiterWorkflowTests(unittest.TestCase):
    def test_job_create_persists_validated_criteria_under_verified_tenant(self) -> None:
        insertions: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID, "email": "recruiter@example.test"})
            if request.url.path == "/rest/v1/organization_memberships":
                return httpx.Response(200, json=[{"role": "recruiter"}])
            if request.url.path == "/rest/v1/jobs" and request.method == "GET":
                return httpx.Response(200, json=[])
            if request.url.path == "/rest/v1/jobs" and request.method == "POST":
                insertions.append(request.read().decode("utf-8"))
                return httpx.Response(201, json=[{
                    "id": JOB_ID,
                    "organization_id": ORG_ID,
                    "title": "Senior analyst",
                    "description": "Build reliable reporting.",
                    "required_skills": ["SQL", "Python"],
                    "preferred_skills": [],
                    "minimum_experience_years": 3,
                    "qualifications": "Equivalent experience accepted.",
                    "responsibilities": "Own analytics.",
                    "status": "active",
                    "created_by": USER_ID,
                    "created_at": "2026-10-30T10:00:00Z",
                    "updated_at": "2026-10-30T10:00:00Z",
                }])
            return httpx.Response(404, json={"code": "not_found"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        api = SupabaseApi(settings(), upstream)
        application = create_app(settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        try:
            with TestClient(application) as client:
                response = client.post(
                    f"/api/v1/organizations/{ORG_ID}/jobs",
                    headers={"Authorization": "Bearer verified-user-token", "Idempotency-Key": str(uuid4())},
                    json={
                        "title": "Senior analyst",
                        "description": "Build reliable reporting.",
                        "required_skills": [" SQL ", "Python", "sql"],
                        "minimum_experience_years": 3,
                        "qualifications": "Equivalent experience accepted.",
                        "responsibilities": "Own analytics.",
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["organization_id"], ORG_ID)
            self.assertEqual(len(insertions), 1)
            self.assertIn('"organization_id":"' + ORG_ID + '"', insertions[0])
            self.assertIn('"created_by":"' + USER_ID + '"', insertions[0])
            self.assertIn('"required_skills":["SQL","Python"]', insertions[0])
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())

    def test_job_create_rejects_client_supplied_ownership(self) -> None:
        with self.assertRaises(ValidationError):
            JobCreateRequest.model_validate({"title": "Role", "description": "Details", "created_by": USER_ID})

    def test_csv_formula_prefix_is_neutralized(self) -> None:
        for value in ("=HYPERLINK(1)", "+SUM(A1:A2)", "-3+4", "@command", "\tformula"):
            with self.subTest(value=value):
                self.assertTrue(_csv_cell(value).startswith("'"))
        buffer = io.StringIO()
        csv.writer(buffer).writerow([_csv_cell("=1+1")])
        self.assertIn("'=1+1", buffer.getvalue())

    def test_job_pdf_extraction_rejects_bad_pdf_before_parser_access(self) -> None:
        from app.exceptions import ApiError

        with self.assertRaises(ApiError) as raised:
            extract_job_description_pdf(b"not a PDF")
        self.assertEqual(raised.exception.code, "invalid_job_description_pdf")

    def test_frontend_renders_untrusted_values_without_html_sinks(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        sources = "\n".join(path.read_text(encoding="utf-8") for path in (root / "static" / "js").glob("*.js"))
        self.assertNotIn("innerHTML", sources)
        self.assertNotIn("insertAdjacentHTML", sources)
        self.assertIn("textContent", sources)


if __name__ == "__main__":
    unittest.main()
