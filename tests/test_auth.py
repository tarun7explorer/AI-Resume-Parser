"""Unit tests for verified recruiter identity; upstream Auth is HTTP-mocked."""

import unittest

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import SupabaseApi
from app.dependencies import get_supabase_api
from app.main import create_app

USER_ID = "5011b33d-4e0d-4b47-8e3e-8c37b9c8b1f2"


class AuthenticationTests(unittest.TestCase):
    def _settings(self) -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            supabase_url="https://unit-test.supabase.co",
            supabase_anon_key="test-public-key",
            supabase_service_role_key="unused-service-role-key",
        )

    def test_me_uses_supabase_verified_user_and_ignores_claim_headers(self) -> None:
        seen_authorization: list[str] = []

        def auth_response(request: httpx.Request) -> httpx.Response:
            seen_authorization.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                json={
                    "id": USER_ID,
                    "email": "recruiter@example.test",
                    "app_metadata": {"role": "untrusted-role"},
                    "user_metadata": {"organization_id": "untrusted-org"},
                },
            )

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(auth_response))
        api = SupabaseApi(self._settings(), upstream)
        application = create_app(self._settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        try:
            with TestClient(application) as client:
                response = client.get(
                    "/api/v1/auth/me",
                    headers={
                        "Authorization": "Bearer signed-user-token",
                        "X-User-ID": "00000000-0000-0000-0000-000000000000",
                        "X-Organization-ID": "untrusted-org",
                        "X-Role": "owner",
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"id": USER_ID, "email": "recruiter@example.test"})
            self.assertEqual(seen_authorization, ["Bearer signed-user-token"])
            self.assertNotIn("untrusted-role", response.text)
            self.assertNotIn("untrusted-org", response.text)
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())

    def test_expired_access_token_returns_safe_401(self) -> None:
        def expired_response(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "JWT expired secret detail"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(expired_response))
        api = SupabaseApi(self._settings(), upstream)
        application = create_app(self._settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        try:
            with TestClient(application) as client:
                response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer expired-token"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_access_token")
            self.assertNotIn("JWT expired secret detail", response.text)
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())

    def test_sign_out_revokes_the_verified_supabase_session(self) -> None:
        paths: list[str] = []

        def responder(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/auth/v1/user":
                return httpx.Response(200, json={"id": USER_ID, "email": "recruiter@example.test"})
            if request.url.path == "/auth/v1/logout":
                self.assertEqual(request.headers.get("authorization"), "Bearer live-access-token")
                return httpx.Response(204)
            return httpx.Response(404)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        api = SupabaseApi(self._settings(), upstream)
        application = create_app(self._settings())
        application.dependency_overrides[get_supabase_api] = lambda: api
        try:
            with TestClient(application) as client:
                response = client.post("/api/v1/auth/sign-out", headers={"Authorization": "Bearer live-access-token"})
            self.assertEqual(response.status_code, 204)
            self.assertEqual(paths, ["/auth/v1/user", "/auth/v1/logout"])
        finally:
            application.dependency_overrides.clear()
            import asyncio

            asyncio.run(upstream.aclose())


if __name__ == "__main__":
    unittest.main()
