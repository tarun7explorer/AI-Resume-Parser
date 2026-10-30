"""Safe error envelopes and request validation behavior."""

import unittest

from fastapi import Query
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = create_app(Settings(_env_file=None, app_env="test"))

    def test_unknown_route_uses_consistent_error_envelope(self) -> None:
        with TestClient(self.application) as client:
            response = client.get("/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "http_error")
        self.assertEqual(response.json()["error"]["message"], "Not Found")
        self.assertTrue(response.json()["error"]["request_id"])

    def test_validation_error_does_not_echo_submitted_value(self) -> None:
        async def validation_probe(limit: int = Query(gt=0)) -> dict[str, int]:
            return {"limit": limit}

        self.application.add_api_route("/_test/validation", validation_probe, methods=["GET"])
        with TestClient(self.application) as client:
            response = client.get("/_test/validation?limit=private-value")
        self.assertEqual(response.status_code, 422)
        payload = response.json()["error"]
        self.assertEqual(payload["code"], "validation_error")
        self.assertNotIn("private-value", response.text)
        self.assertNotIn("input", payload["details"][0])

    def test_internal_error_is_generic(self) -> None:
        async def failing_probe() -> None:
            raise RuntimeError("sensitive exception detail")

        self.application.add_api_route("/_test/failure", failing_probe, methods=["GET"])
        with TestClient(self.application, raise_server_exceptions=False) as client:
            response = client.get("/_test/failure")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("sensitive exception detail", response.text)


if __name__ == "__main__":
    unittest.main()
