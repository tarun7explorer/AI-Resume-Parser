"""Settings defaults and fail-closed validation tests."""

import unittest

from pydantic import ValidationError

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_development_defaults_are_safe(self) -> None:
        settings = Settings(_env_file=None, app_env="development")
        self.assertTrue(settings.debug)
        self.assertEqual(settings.port, 8000)
        self.assertIsNone(settings.supabase_service_role_key)

    def test_example_environment_contains_no_active_credentials(self) -> None:
        settings = Settings(_env_file=".env.example")
        self.assertIsNone(settings.supabase_url)
        self.assertIsNone(settings.groq_api_key)

    def test_production_debug_is_disabled(self) -> None:
        settings = Settings(
            _env_file=None,
            app_env="production",
            supabase_url="https://example.supabase.co",
            supabase_anon_key="public-test-key",
            supabase_service_role_key="server-test-key",
            groq_api_key="provider-test-key",
            screening_worker_enabled=True,
        )
        self.assertFalse(settings.debug)

    def test_rejects_wildcard_cors(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, cors_origins=["*"])

    def test_production_rejects_http_cors_origin(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                app_env="production",
                supabase_url="https://example.supabase.co",
                supabase_anon_key="public-test-key",
                cors_origins=["http://localhost:3000"],
            )

    def test_requires_supabase_url_and_anon_key_together(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, supabase_url="https://example.supabase.co")

    def test_service_role_key_is_optional_for_user_scoped_access(self) -> None:
        settings = Settings(
            _env_file=None,
            supabase_url="https://example.supabase.co",
            supabase_anon_key="public-test-key",
        )
        self.assertIsNone(settings.supabase_service_role_key)

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, port=70000)


if __name__ == "__main__":
    unittest.main()
