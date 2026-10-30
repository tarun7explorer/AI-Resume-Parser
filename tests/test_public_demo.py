"""Public dashboard and stateless screening endpoint contracts."""

from __future__ import annotations

import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.report_models import (
    CandidateAnalysisAI,
    EvidenceSpan,
    JobRequirements,
    ProfileFact,
    RequirementAssessment,
)
from app.resume_parser import PageText, ParsedDocument


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        supabase_url="https://unit-test.supabase.co",
        supabase_anon_key="public-test-key",
        groq_api_key="provider-test-key",
    )


def _requirements() -> JobRequirements:
    return JobRequirements(
        required_skills=["Python"],
        preferred_skills=[],
        experience_expectations=["Build APIs"],
        responsibilities=[],
        qualifications=[],
        minimum_experience_years=None,
    )


def _analysis() -> CandidateAnalysisAI:
    return CandidateAnalysisAI(
        skills=[ProfileFact(
            statement="Python engineer",
            certainty="explicit",
            evidence=[EvidenceSpan(page=1, quote="Python engineer")],
        )],
        experience_projects=[ProfileFact(
            statement="Built APIs",
            certainty="explicit",
            evidence=[EvidenceSpan(page=1, quote="building APIs")],
        )],
        education_qualifications=[],
        assessments=[
            RequirementAssessment(
                category="required_skills", requirement="Python", status="supported",
                certainty="explicit", explanation="Named directly.",
                evidence=[EvidenceSpan(page=1, quote="Python engineer")],
            ),
            RequirementAssessment(
                category="experience_projects", requirement="Build APIs", status="supported",
                certainty="explicit", explanation="Described directly.",
                evidence=[EvidenceSpan(page=1, quote="building APIs")],
            ),
        ],
    )


def _document() -> ParsedDocument:
    text = "Python engineer with five years building APIs."
    return ParsedDocument((PageText(1, text, "text"),), (), 1, False, text)


class PublicDemoTests(unittest.TestCase):
    def test_public_dashboard_has_no_login_gate_or_auth_controls(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        with TestClient(application) as client:
            root = client.get("/")
            dashboard = client.get("/dashboard")
            old_screening_link = client.get("/screening")
            old_candidate_link = client.get("/candidate")

        for response in (root, dashboard, old_screening_link, old_candidate_link):
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="demo-form"', response.text)
            self.assertNotIn("sign-in-form", response.text)
            self.assertNotIn("sign-out", response.text.lower())
        self.assertIn('id="privacy-acknowledged"', root.text)

    def test_demo_results_are_request_scoped_without_auth_cookies_or_db_access(self) -> None:
        application = create_app(_settings())
        endpoint = "app.api_demo.GroqProvider.extract_job_requirements"
        candidate = "app.api_demo.GroqProvider.extract_candidate"
        payloads = [b"%PDF-1.7\nfirst\n%%EOF", b"%PDF-1.7\nsecond\n%%EOF"]
        with (
            patch(endpoint, new=AsyncMock(return_value=_requirements())),
            patch(candidate, new=AsyncMock(return_value=_analysis())),
            patch("app.api_demo.extract_pdf", return_value=_document()),
            TestClient(application) as client,
        ):
            responses = [
                client.post(
                    "/api/v1/demo/screenings",
                    data={"job_description": "Python engineer. Build APIs.", "privacy_acknowledged": "true"},
                    files={"resumes": (f"candidate-{index}.pdf", content, "application/pdf")},
                )
                for index, content in enumerate(payloads, start=1)
            ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        reports = [response.json()["results"][0]["report"] for response in responses]
        self.assertEqual(
            [report["source"]["sha256"] for report in reports],
            [hashlib.sha256(content).hexdigest() for content in payloads],
        )
        self.assertEqual([report["job_match_score"] for report in reports], [10, 10])
        self.assertTrue(all("set-cookie" not in response.headers for response in responses))

    def test_demo_requires_consent_and_rejects_non_pdf_files(self) -> None:
        application = create_app(_settings())
        with (
            patch("app.api_demo.GroqProvider.extract_job_requirements", new=AsyncMock(return_value=_requirements())),
            TestClient(application) as client,
        ):
            missing_consent = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python engineer."},
                files={"resumes": ("candidate.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
            )
            invalid_pdf = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python engineer.", "privacy_acknowledged": "true"},
                files={"resumes": ("candidate.txt", b"not a pdf", "text/plain")},
            )

        self.assertEqual(missing_consent.status_code, 422)
        self.assertEqual(invalid_pdf.status_code, 200)
        result = invalid_pdf.json()["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["report"])

    def test_demo_accepts_uploaded_job_description_pdf_and_resume(self) -> None:
        application = create_app(_settings())
        with (
            patch("app.api_demo.extract_job_description_pdf", return_value="Python engineer. Build APIs.") as extract_job_pdf,
            patch("app.api_demo.GroqProvider.extract_job_requirements", new=AsyncMock(return_value=_requirements())),
            patch("app.api_demo.GroqProvider.extract_candidate", new=AsyncMock(return_value=_analysis())),
            patch("app.api_demo.extract_pdf", return_value=_document()),
            TestClient(application) as client,
        ):
            response = client.post(
                "/api/v1/demo/screenings",
                data={"privacy_acknowledged": "true"},
                files=[
                    ("resumes", ("candidate.pdf", b"%PDF-1.7\nresume\n%%EOF", "application/pdf")),
                    ("job_description_pdf", ("role.pdf", b"%PDF-1.7\nrole\n%%EOF", "application/pdf")),
                ],
            )

        self.assertEqual(response.status_code, 200, response.text)
        extract_job_pdf.assert_called_once_with(b"%PDF-1.7\nrole\n%%EOF", max_pages=100)
        self.assertEqual(response.json()["results"][0]["status"], "completed")

    def test_demo_rejects_missing_description_and_unexpected_errors_are_not_hidden(self) -> None:
        application = create_app(_settings())
        with TestClient(application) as client:
            missing_description = client.post(
                "/api/v1/demo/screenings",
                data={"privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", b"%PDF-1.7\nresume\n%%EOF", "application/pdf")},
            )
        self.assertEqual(missing_description.status_code, 422)
        self.assertEqual(missing_description.json()["error"]["code"], "job_description_required")

        with (
            patch("app.api_demo.GroqProvider.extract_job_requirements", new=AsyncMock(return_value=_requirements())),
            patch("app.api_demo.extract_pdf", return_value=_document()),
            patch("app.api_demo.GroqProvider.extract_candidate", new=AsyncMock(return_value=_analysis())),
            patch("app.api_demo.score_candidate", side_effect=RuntimeError("internal processing failure")),
            TestClient(application, raise_server_exceptions=False) as client,
        ):
            unexpected = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python engineer.", "privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", b"%PDF-1.7\nresume\n%%EOF", "application/pdf")},
            )
        self.assertEqual(unexpected.status_code, 500)
        self.assertEqual(unexpected.json()["error"]["code"], "internal_error")
        self.assertNotIn("internal processing failure", unexpected.text)

    def test_unknown_api_route_explains_that_the_running_app_may_be_stale(self) -> None:
        application = create_app(_settings())
        with TestClient(application) as client:
            response = client.post("/api/v1/demo/screening")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "api_endpoint_not_found")
        self.assertIn("Restart TalentLens", response.json()["error"]["message"])

    def test_existing_tenant_api_remains_protected(self) -> None:
        application = create_app(_settings())
        with TestClient(application) as client:
            response = client.get("/api/v1/organizations")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")


if __name__ == "__main__":
    unittest.main()
