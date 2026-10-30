"""Shared test configuration and deterministic environment settings."""

import pytest

from app.config import Settings


@pytest.fixture
def test_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", cors_origins=[])
