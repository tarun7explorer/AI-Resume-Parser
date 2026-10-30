"""Unit tests for Supabase HTTP error mapping and user-scoped bearer forwarding."""

import unittest

import httpx

from app.config import Settings
from app.database import SupabaseApi
from app.exceptions import ApiError


def test_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "app_env": "test",
        "supabase_url": "https://unit-test.supabase.co",
        "supabase_anon_key": "public-anon-test-key",
        "supabase_service_role_key": "must-never-be-used",
    }
    values.update(overrides)
    return Settings(**values)


class SupabaseClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgrest_uses_anon_key_and_user_token_not_service_role(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update({key.lower(): value for key, value in request.headers.items()})
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = SupabaseApi(test_settings(), client)
            result = await api.select("jobs", "verified-user-jwt", params={"select": "id"})

        self.assertEqual(result, [])
        self.assertEqual(seen["apikey"], "public-anon-test-key")
        self.assertEqual(seen["authorization"], "Bearer verified-user-jwt")
        self.assertNotIn("must-never-be-used", str(seen))

    async def test_rls_permission_error_is_mapped_without_provider_details(self) -> None:
        def denied(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"code": "42501", "message": "private database detail"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
            api = SupabaseApi(test_settings(), client)
            with self.assertRaises(ApiError) as raised:
                await api.select("jobs", "verified-user-jwt", params={"select": "id"})

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertNotIn("private database detail", str(raised.exception))

    async def test_unavailable_provider_is_mapped_to_503(self) -> None:
        def unavailable(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "provider body is not exposed"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            api = SupabaseApi(test_settings(), client)
            with self.assertRaises(ApiError) as raised:
                await api.verify_access_token("signed-user-jwt")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "supabase_unavailable")
        self.assertNotIn("provider body", str(raised.exception))

    async def test_missing_auth_configuration_fails_clearly(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(ApiError) as raised:
                SupabaseApi(Settings(_env_file=None, app_env="test"), client)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "supabase_not_configured")


if __name__ == "__main__":
    unittest.main()
