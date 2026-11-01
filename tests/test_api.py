from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class NoDatabase:
    def open(self): pass
    def close(self): pass
    def ping(self): return True


def client():
    return TestClient(create_app(Settings(database_url="postgresql://u:p@localhost/db"), NoDatabase()))


def test_health_is_safe_and_database_independent():
    with client() as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_requires_bearer_before_repository_access():
    with client() as c:
        response = c.get("/api/status/00000000-0000-0000-0000-000000000001")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_submission_rejects_both_job_description_sources():
    with client() as c:
        response = c.post(
            "/api/screenings",
            data={"job_description_text": "A sufficiently long job description."},
            files={
                "job_description_pdf": ("jd.pdf", b"%PDF-fake", "application/pdf"),
                "resumes": ("resume.pdf", b"%PDF-fake", "application/pdf"),
            },
        )
    assert response.status_code == 422
    assert "exactly one job description" in response.json()["error"]["message"].lower()
