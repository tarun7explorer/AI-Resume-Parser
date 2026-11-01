"""Hosted LLM client (Groq or NVIDIA OpenAI-compatible chat completions).

Behavior summary
----------------
* Credentials and settings come only from server-side environment variables.
* One provider and one model, exactly as configured. There is NO model
  fallback and NO fabricated/keyword fallback result: on failure a typed
  ``AIProviderError`` is raised.
* Bounded per-request timeout, bounded attempts, exponential backoff with
  jitter, ``Retry-After`` handling for HTTP 429.
* Errors carry a stable ``code`` and a fixed public message. Provider response
  bodies, prompts, resume text and keys are never placed in exceptions or logs.
* Output is parsed as JSON and validated with ``AIAssessment``.

This client is synchronous (``httpx.Client``). If the worker runs in an async
loop, call it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import httpx
from pydantic import ValidationError

from app.ai_schemas import AIAssessment, AIAssessmentResult, PageText
from app.prompts import PROMPT_VERSION, build_messages

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class AIProviderError(Exception):
    """Base class. ``str(exc)`` is only the stable code; it is always safe to log."""

    code = "ai_provider_error"
    public_message = "The AI provider request failed."
    retryable = False  # True = transient; a later attempt/job retry may succeed.

    def __init__(
        self,
        *,
        http_status: int | None = None,
        retry_after: float | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(self.code)
        self.http_status = http_status
        self.retry_after = retry_after
        self.attempts = attempts

    def to_public_dict(self) -> dict[str, Any]:
        """Sanitized error payload suitable for persisting and showing to users."""
        return {
            "code": self.code,
            "message": self.public_message,
            "retryable": self.retryable,
        }


class AIConfigurationError(AIProviderError):
    code = "ai_not_configured"
    public_message = "The AI provider is not configured correctly."


class AIInputTooLargeError(AIProviderError):
    code = "ai_input_too_large"
    public_message = "The job description and resume are too large to analyze."


class AIAuthenticationError(AIProviderError):
    code = "ai_auth_failed"
    public_message = "The AI provider rejected the server credentials."


class AIRateLimitError(AIProviderError):
    code = "ai_rate_limited"
    public_message = "The AI provider rate limit was reached. Try again later."
    retryable = True


class AITimeoutError(AIProviderError):
    code = "ai_timeout"
    public_message = "The AI provider did not respond in time."
    retryable = True


class AIConnectionError(AIProviderError):
    code = "ai_connection_error"
    public_message = "The AI provider could not be reached."
    retryable = True


class AIServerError(AIProviderError):
    code = "ai_server_error"
    public_message = "The AI provider reported a temporary server error."
    retryable = True


class AIRequestRejectedError(AIProviderError):
    code = "ai_request_rejected"
    public_message = (
        "The AI provider rejected the request. Check the configured model and provider."
    )


class AIOutputTruncatedError(AIProviderError):
    code = "ai_output_truncated"
    public_message = "The AI response was cut off before it was complete."


class AIMalformedJSONError(AIProviderError):
    code = "ai_malformed_json"
    public_message = "The AI provider returned a response that was not valid JSON."
    retryable = True


class AISchemaValidationError(AIProviderError):
    code = "ai_invalid_schema"
    public_message = "The AI response did not match the required structure."
    retryable = True


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _ProviderPreset:
    base_url: str
    key_env: str
    default_json_mode: str


_PRESETS: dict[str, _ProviderPreset] = {
    "groq": _ProviderPreset("https://api.groq.com/openai/v1", "GROQ_API_KEY", "json_object"),
    "nvidia": _ProviderPreset("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "none"),
}
_JSON_MODES = {"json_object", "none"}


def _env_number(
    env: Mapping[str, str],
    name: str,
    default: float,
    lo: float,
    hi: float,
    *,
    integer: bool,
) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return int(default) if integer else default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise AIConfigurationError() from None
    if not (lo <= value <= hi) or value != value:
        raise AIConfigurationError()
    return value


@dataclass(frozen=True)
class AISettings:
    """Validated AI configuration. ``repr`` never includes the API key."""

    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str
    timeout_seconds: float
    connect_timeout_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    max_retry_after_seconds: float
    max_output_tokens: int
    temperature: float
    json_mode: str
    max_input_chars: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AISettings":
        """Load and validate settings. Raises ``AIConfigurationError`` (no secrets in message)."""
        env = os.environ if env is None else env

        provider = (env.get("AI_PROVIDER") or "").strip().lower()
        if provider not in _PRESETS:
            logger.error("Invalid or missing AI_PROVIDER (allowed: %s)", ", ".join(_PRESETS))
            raise AIConfigurationError()
        preset = _PRESETS[provider]

        model = (env.get("AI_MODEL") or "").strip()
        if not model or model.startswith("replace-with"):
            logger.error("AI_MODEL is not set; configure a model ID your provider account supports")
            raise AIConfigurationError()

        api_key = (env.get(preset.key_env) or "").strip()
        if not api_key or api_key.startswith("replace-"):
            logger.error("%s is not set for provider %s", preset.key_env, provider)
            raise AIConfigurationError()

        base_url = (env.get("AI_BASE_URL") or preset.base_url).strip().rstrip("/")
        if not base_url.startswith("https://"):
            logger.error("AI_BASE_URL must use https")
            raise AIConfigurationError()

        json_mode = (env.get("AI_JSON_MODE") or preset.default_json_mode).strip().lower()
        if json_mode not in _JSON_MODES:
            logger.error("AI_JSON_MODE must be one of: %s", ", ".join(sorted(_JSON_MODES)))
            raise AIConfigurationError()

        try:
            timeout = float(_env_number(env, "AI_TIMEOUT_SECONDS", 60.0, 5.0, 300.0, integer=False))
            return cls(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=timeout,
                connect_timeout_seconds=min(10.0, timeout),
                max_attempts=int(_env_number(env, "AI_MAX_ATTEMPTS", 3, 1, 6, integer=True)),
                backoff_base_seconds=float(
                    _env_number(env, "AI_BACKOFF_BASE_SECONDS", 1.0, 0.0, 30.0, integer=False)
                ),
                backoff_max_seconds=float(
                    _env_number(env, "AI_BACKOFF_MAX_SECONDS", 20.0, 0.0, 120.0, integer=False)
                ),
                max_retry_after_seconds=float(
                    _env_number(env, "AI_MAX_RETRY_AFTER_SECONDS", 30.0, 0.0, 300.0, integer=False)
                ),
                max_output_tokens=int(
                    _env_number(env, "AI_MAX_OUTPUT_TOKENS", 3000, 256, 32000, integer=True)
                ),
                temperature=float(_env_number(env, "AI_TEMPERATURE", 0.0, 0.0, 1.0, integer=False)),
                json_mode=json_mode,
                max_input_chars=int(
                    _env_number(env, "AI_MAX_INPUT_CHARS", 60000, 1000, 400000, integer=True)
                ),
            )
        except AIConfigurationError:
            logger.error("A numeric AI_* setting is invalid or out of range")
            raise


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_json_text(content: str) -> str:
    """Remove reasoning blocks and a single surrounding code fence, nothing more."""
    text = _THINK_RE.sub("", content).strip()
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return max(0.0, seconds) if seconds == seconds else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class AIProvider:
    """Client for one configured provider/model."""

    def __init__(
        self,
        settings: AISettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=False)
        self._sleep = sleep
        self._rng = rng

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AIProvider":
        return cls(AISettings.from_env(env))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AIProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def assess_resume(
        self, job_description: str, resume_pages: Sequence[PageText]
    ) -> AIAssessmentResult:
        """Return a schema-validated assessment, or raise ``AIProviderError``.

        The returned quotes/pages are PROPOSALS. The caller must verify them
        with ``app/evidence.py`` and compute scores with ``app/scoring.py``.

        Raises:
            ValueError: if the job description or resume text is empty.
            AIInputTooLargeError: if the combined input exceeds AI_MAX_INPUT_CHARS.
                (Input is never silently truncated.)
            AIProviderError: any provider/parse/validation failure after bounded retries.
        """
        if not job_description.strip():
            raise ValueError("job_description must not be empty")
        if not any(p.text.strip() for p in resume_pages):
            raise ValueError("resume has no extractable text")
        total = len(job_description) + sum(len(p.text) for p in resume_pages)
        if total > self.settings.max_input_chars:
            raise AIInputTooLargeError()

        messages = build_messages(job_description, resume_pages)
        last_error: AIProviderError | None = None

        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                assessment, usage = self._attempt(messages)
                return AIAssessmentResult(
                    assessment=assessment,
                    provider=self.settings.provider,
                    model=self.settings.model,
                    prompt_version=PROMPT_VERSION,
                    attempts=attempt,
                    prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                    completion_tokens=_int_or_none(usage.get("completion_tokens")),
                )
            except AIProviderError as exc:
                exc.attempts = attempt
                last_error = exc
                logger.warning(
                    "AI attempt failed provider=%s code=%s http_status=%s attempt=%d/%d",
                    self.settings.provider,
                    exc.code,
                    exc.http_status,
                    attempt,
                    self.settings.max_attempts,
                )
                if not exc.retryable or attempt >= self.settings.max_attempts:
                    raise
                delay = self._delay(attempt, exc.retry_after)
                if delay is None:  # Retry-After longer than our cap: let the job layer reschedule.
                    raise
                self._sleep(delay)

        assert last_error is not None  # pragma: no cover - loop always returns or raises
        raise last_error  # pragma: no cover

    # -- internals ---------------------------------------------------------
    def _delay(self, attempt: int, retry_after: float | None) -> float | None:
        s = self.settings
        backoff = min(s.backoff_max_seconds, s.backoff_base_seconds * (2 ** (attempt - 1)))
        backoff *= 0.5 + 0.5 * self._rng()  # jitter in [0.5x, 1.0x]
        if retry_after is not None:
            if retry_after > s.max_retry_after_seconds:
                return None
            return max(retry_after, backoff)
        return backoff

    def _attempt(self, messages: list[dict[str, str]]) -> tuple[AIAssessment, dict[str, Any]]:
        content, usage = self._request(messages)
        text = _extract_json_text(content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise AIMalformedJSONError() from None
        if not isinstance(data, dict):
            raise AIMalformedJSONError()
        try:
            return AIAssessment.model_validate(data), usage
        except ValidationError as exc:
            # Log only field locations and error types; pydantic errors can echo input values.
            where = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors()})
            logger.warning("AI schema validation failed fields=%s", where[:10])
            raise AISchemaValidationError() from None

    def _request(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        s = self.settings
        body: dict[str, Any] = {
            "model": s.model,
            "messages": messages,
            "temperature": s.temperature,
            "max_tokens": s.max_output_tokens,
            "stream": False,
        }
        if s.json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(
                f"{s.base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {s.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(s.timeout_seconds, connect=s.connect_timeout_seconds),
            )
        except httpx.TimeoutException:
            raise AITimeoutError() from None
        except httpx.TransportError:
            raise AIConnectionError() from None

        status = response.status_code
        if status in (401, 403):
            raise AIAuthenticationError(http_status=status)
        if status == 429:
            raise AIRateLimitError(
                http_status=status,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        if status == 408:
            raise AITimeoutError(http_status=status)
        if status >= 500:
            raise AIServerError(http_status=status)
        if status != 200:
            raise AIRequestRejectedError(http_status=status)

        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            usage = payload.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AIMalformedJSONError(http_status=status) from None

        if finish_reason == "length":
            raise AIOutputTruncatedError(http_status=status)
        if not isinstance(content, str) or not content.strip():
            raise AIMalformedJSONError(http_status=status)
        return content, usage if isinstance(usage, dict) else {}