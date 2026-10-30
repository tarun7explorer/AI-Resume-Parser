"""Application initialization, liveness, and response headers."""

import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = create_app(Settings(_env_file=None, app_env="test"))

    def test_startup_and_health_endpoint(self) -> None:
        with TestClient(self.application) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers.get("content-security-policy", ""))

    def test_root_page_is_served(self) -> None:
        with TestClient(self.application) as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Welcome back", response.text)
        self.assertIn("sign-in-form", response.text)


if __name__ == "__main__":
    unittest.main()
