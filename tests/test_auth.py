"""No-login API surface and verified tenant API access requirements."""

import unittest

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import SupabaseApi
from app.dependencies import get_supabase_api
from app.main import create_app

USER_ID = "5011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"


class AuthenticationRouteRemovalTests(unittest.TestCase):
    def _settings(self) -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            supabase_url="https://unit-test.supabase.co",
            supabase_anon_key="public-test-key",
        )

    def test_organization_membership_uses_supabase_verified_identity(self) -> None:
        seen_authorization: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            seen_authorization.append(request.headers.get("authorization", ""))
            if request.url.path == "/auth/v1/user":
                return httpx.Response(
                    200,
                    json={
                        "id": USER_ID,
                        "email": "recruiter@example.test",
                        "app_metadata": {"role": "untrusted-role"},
                        "user_metadata": {"organization_id": "untrusted-org"},
                    },
                )
            if request.url.path == "/rest/v1/organization_memberships":
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        application = create_app(self._settings())
        application.dependency_overrides[get_supabase_api] = lambda: SupabaseApi(self._settings(), upstream)
        try:
            with TestClient(application) as client:
                response = client.get(
                    "/api/v1/organizations",
                    headers={
                        "Authorization": "Bearer signed-user-token",
                        "X-User-ID": "00000000-0000-0000-0000-000000000000",
                        "X-Organization-ID": "untrusted-org",
                        "X-Role": "owner",
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [])
            self.assertEqual(seen_authorization, ["Bearer signed-user-token", "Bearer signed-user-token"])
            self.assertNotIn("untrusted-role", response.text)
            self.assertNotIn("untrusted-org", response.text)
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())

    def test_expired_access_token_returns_safe_401_for_protected_data(self) -> None:
        def expired_response(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "JWT expired secret detail"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(expired_response))
        application = create_app(self._settings())
        application.dependency_overrides[get_supabase_api] = lambda: SupabaseApi(self._settings(), upstream)
        try:
            with TestClient(application) as client:
                response = client.get("/api/v1/organizations", headers={"Authorization": "Bearer expired-token"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_access_token")
            self.assertNotIn("JWT expired secret detail", response.text)
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())

    def test_session_management_endpoints_are_not_exposed(self) -> None:
        application = create_app(Settings(_env_file=None, app_env="test"))
        with TestClient(application) as client:
            responses = [
                client.post("/api/v1/auth/sign-in", json={"email": "a@example.test", "password": "value"}),
                client.post("/api/v1/auth/refresh", json={"refresh_token": "value"}),
                client.get("/api/v1/auth/me"),
                client.post("/api/v1/auth/sign-out"),
            ]
        self.assertTrue(all(response.status_code == 404 for response in responses))

    def test_legacy_tenant_data_remains_protected(self) -> None:
        application = create_app(Settings(
            _env_file=None,
            app_env="test",
            supabase_url="https://unit-test.supabase.co",
            supabase_anon_key="public-test-key",
        ))
        with TestClient(application) as client:
            response = client.get("/api/v1/organizations")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")


if __name__ == "__main__":
    unittest.main()
