"""Public AI screening route integration tests with a mocked provider boundary."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.report_models import JobRequirements


def _pdf(text: str) -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


def _provider_responses() -> list[str]:
    # Omitted empty arrays are defaulted safely by the AI response models.
    requirements = {
        "required_skills": ["Python"],
        "experience_expectations": ["Build APIs"],
    }
    candidate = {
        "skills": [{
            "statement": "Python engineer",
            "certainty": "explicit",
            "evidence": [{"page": 1, "quote": "Python"}],
        }],
        "assessments": [
            {
                "category": "required_skills", "requirement": "Python", "status": "supported",
                "certainty": "explicit", "explanation": "The skill is named in the resume.",
                "evidence": [{"page": 1, "quote": "Python"}],
            },
            {
                "category": "experience_projects", "requirement": "Build APIs", "status": "supported",
                "certainty": "explicit", "explanation": "API building is stated in the resume.",
                "evidence": [{"page": 1, "quote": "building APIs"}],
            },
        ],
    }
    return [json.dumps(requirements), json.dumps(candidate)]


class PublicDemoTests(unittest.TestCase):
    def test_dashboard_and_real_screening_route_return_frontend_schema(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        with (
            patch("app.ai.GroqProvider._completion", new=AsyncMock(side_effect=_provider_responses())) as provider_call,
            TestClient(application) as client,
        ):
            for path in ("/", "/dashboard", "/screening", "/candidate"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn('id="demo-form"', page.text)
            response = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python required; build APIs.", "privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", _pdf("Python engineer with five years building APIs."), "application/pdf")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(provider_call.await_count, 2)
        result = response.json()["results"][0]
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["matched_skills"], ["Python"])
        self.assertEqual(result["missing_requirements"], [])
        report = result["report"]
        self.assertEqual(report["job_match_score"], 10)
        self.assertTrue(report["profile"]["assessments"])
        self.assertIn("category_breakdown", report)
        self.assertTrue(any("Scores are provisional" in text for text in report["limitations"]))
        self.assertNotIn("set-cookie", response.headers)

    def test_provider_schema_failure_returns_safe_503_without_raw_output(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        with (
            patch("app.ai.GroqProvider._completion", new=AsyncMock(return_value='{"private": "provider payload"}')),
            TestClient(application) as client,
        ):
            response = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python required", "privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", _pdf("Python engineer with five years building APIs."), "application/pdf")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "ai_output_schema_invalid")
        self.assertIn("required response schema", response.json()["error"]["message"])
        self.assertNotIn("provider payload", response.text)

    def test_candidate_evidence_failure_is_reported_without_resume_text(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        responses = _provider_responses()
        invalid_candidate = json.loads(responses[1])
        invalid_candidate["assessments"][0]["evidence"][0]["quote"] = "private resume detail"
        with (
            patch(
                "app.ai.GroqProvider._completion",
                new=AsyncMock(side_effect=[responses[0], json.dumps(invalid_candidate), json.dumps(invalid_candidate)]),
            ),
            TestClient(application) as client,
        ):
            response = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python required; build APIs.", "privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", _pdf("Python engineer with five years building APIs."), "application/pdf")},
            )
        self.assertEqual(response.status_code, 200)
        result = response.json()["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["report"])
        self.assertIn("did not match the extracted resume pages", result["message"])
        self.assertNotIn("private resume detail", response.text)

    def test_consent_and_job_description_are_required(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        with TestClient(application) as client:
            missing_consent = client.post(
                "/api/v1/demo/screenings",
                data={"job_description": "Python developer"},
                files={"resumes": ("candidate.pdf", _pdf("Python engineer and API developer."), "application/pdf")},
            )
            missing_description = client.post(
                "/api/v1/demo/screenings",
                data={"privacy_acknowledged": "true"},
                files={"resumes": ("candidate.pdf", _pdf("Python engineer and API developer."), "application/pdf")},
            )
        self.assertEqual(missing_consent.status_code, 422)
        self.assertEqual(missing_description.status_code, 422)
        self.assertEqual(missing_description.json()["error"]["code"], "job_description_required")

    def test_invalid_empty_and_unreadable_resume_fail_with_actionable_result(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        unreadable = fitz.open()
        unreadable.new_page()
        unreadable_pdf = unreadable.tobytes()
        unreadable.close()
        cases = [("invalid.pdf", b"not pdf", "application/pdf"),
                 ("resume.txt", b"Python engineer", "text/plain"),
                 ("empty.pdf", b"", "application/pdf"),
                 ("blank.pdf", unreadable_pdf, "application/pdf")]
        with (
            patch(
                "app.api_demo.GroqProvider.extract_job_requirements",
                new=AsyncMock(return_value=JobRequirements(
                    required_skills=["Python"], preferred_skills=[], experience_expectations=[],
                    responsibilities=[], qualifications=[], minimum_experience_years=None,
                )),
            ),
            TestClient(application) as client,
        ):
            results = []
            for filename, content, mime in cases:
                response = client.post(
                    "/api/v1/demo/screenings",
                    data={"job_description": "Python required; build APIs.", "privacy_acknowledged": "true"},
                    files={"resumes": (filename, content, mime)},
                )
                self.assertEqual(response.status_code, 200, response.text)
                results.append(response.json()["results"][0])
        self.assertTrue(all(result["status"] == "failed" and result["message"] for result in results))
        self.assertIn("valid PDF", results[1]["message"])
        self.assertIn("empty", results[2]["message"].lower())
        self.assertIn("readable text", results[3]["message"].lower())


if __name__ == "__main__":
    unittest.main()
