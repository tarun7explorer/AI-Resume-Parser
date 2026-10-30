"""Validated environment-backed settings for local and deployed runtimes."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration; secrets are represented as ``SecretStr``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
        validate_default=True,
    )

    app_name: str = "AI Resume Screening & Candidate Intelligence"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=list)

    supabase_url: AnyHttpUrl | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None
    resume_max_bytes: int = 10 * 1024 * 1024
    signed_url_ttl_seconds: int = 60
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-20b"
    nvidia_api_key: SecretStr | None = None
    screening_worker_enabled: bool = False
    pdf_max_pages: int = 100
    ocr_max_pages: int = 20
    ocr_timeout_seconds: int = 15
    ocr_language: str = "eng"
    tesseract_cmd: str | None = None
    ai_timeout_seconds: int = 45
    ai_max_attempts: int = 2
    classification_considered_min: int = 40
    classification_priority_min: int = 75

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return value

    @field_validator("resume_max_bytes")
    @classmethod
    def validate_resume_max_bytes(cls, value: int) -> int:
        if not 1024 <= value <= 10 * 1024 * 1024:
            raise ValueError("RESUME_MAX_BYTES must be between 1 KiB and 10 MiB")
        return value

    @field_validator("signed_url_ttl_seconds")
    @classmethod
    def validate_signed_url_ttl(cls, value: int) -> int:
        if not 30 <= value <= 300:
            raise ValueError("SIGNED_URL_TTL_SECONDS must be between 30 and 300")
        return value

    @field_validator("pdf_max_pages")
    @classmethod
    def validate_pdf_max_pages(cls, value: int) -> int:
        if not 1 <= value <= 250:
            raise ValueError("PDF_MAX_PAGES must be between 1 and 250")
        return value

    @field_validator("ocr_max_pages")
    @classmethod
    def validate_ocr_max_pages(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("OCR_MAX_PAGES must be between 1 and 50")
        return value

    @field_validator("ocr_timeout_seconds")
    @classmethod
    def validate_ocr_timeout(cls, value: int) -> int:
        if not 1 <= value <= 60:
            raise ValueError("OCR_TIMEOUT_SECONDS must be between 1 and 60")
        return value

    @field_validator("ai_timeout_seconds")
    @classmethod
    def validate_ai_timeout(cls, value: int) -> int:
        if not 5 <= value <= 180:
            raise ValueError("AI_TIMEOUT_SECONDS must be between 5 and 180")
        return value

    @field_validator("ai_max_attempts")
    @classmethod
    def validate_ai_attempts(cls, value: int) -> int:
        if not 1 <= value <= 3:
            raise ValueError("AI_MAX_ATTEMPTS must be between 1 and 3")
        return value

    @model_validator(mode="after")
    def validate_classification_bands(self) -> "Settings":
        if not 1 <= self.classification_considered_min < self.classification_priority_min <= 100:
            raise ValueError("Classification band thresholds must satisfy 1 <= CONSIDERED < PRIORITY <= 100")
        return self

    @field_validator("groq_model")
    @classmethod
    def validate_groq_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128 or any(char.isspace() for char in value):
            raise ValueError("GROQ_MODEL must be a model identifier")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            origin = value.strip().rstrip("/")
            parsed = urlsplit(origin)
            if origin == "*" or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("CORS_ORIGINS must contain explicit HTTP(S) origins; wildcards are forbidden")
            if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("CORS_ORIGINS entries must be origins without paths or credentials")
            normalized.append(origin)
        return normalized

    @model_validator(mode="after")
    def validate_integrations(self) -> "Settings":
        if (self.supabase_url is None) != (self.supabase_anon_key is None):
            raise ValueError("SUPABASE_URL and SUPABASE_ANON_KEY must be configured together")
        if self.supabase_service_role_key is not None and self.supabase_url is None:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY requires SUPABASE_URL and SUPABASE_ANON_KEY")
        if self.screening_worker_enabled:
            if self.supabase_url is None or self.supabase_service_role_key is None:
                raise ValueError("SCREENING_WORKER_ENABLED requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
            if self.groq_api_key is None:
                raise ValueError("SCREENING_WORKER_ENABLED requires GROQ_API_KEY")

        if self.app_env == "production":
            if self.supabase_url is None or self.supabase_anon_key is None:
                raise ValueError("production requires SUPABASE_URL and SUPABASE_ANON_KEY")
            if any(urlsplit(origin).scheme != "https" for origin in self.cors_origins):
                raise ValueError("production CORS_ORIGINS must use HTTPS")
            if self.supabase_url is not None and self.supabase_url.scheme != "https":
                raise ValueError("production SUPABASE_URL must use HTTPS")
            if not self.screening_worker_enabled:
                raise ValueError("production requires SCREENING_WORKER_ENABLED=true")
            if self.supabase_service_role_key is None:
                raise ValueError("the screening worker requires SUPABASE_SERVICE_ROLE_KEY")
            if self.groq_api_key is None:
                raise ValueError("the screening worker requires GROQ_API_KEY")
        return self

    @property
    def debug(self) -> bool:
        """Enable framework debug behavior only in local development."""

        return self.app_env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached process settings after validating environment variables."""

    return Settings()
