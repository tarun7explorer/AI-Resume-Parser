"""Safe, structured HTTP application errors."""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, message: str = "Request failed.", *, code: str = "request_failed", status_code: int = 400, headers=None, details=None):
        super().__init__(message)
        self.message, self.code, self.status_code = message, code, status_code
        self.headers, self.details = headers, details


class ValidationFailed(AppError):
    def __init__(self, message="The request is invalid.", **kw): super().__init__(message, code="validation_error", status_code=422, **kw)
class PayloadTooLarge(AppError):
    def __init__(self, message="The request is too large.", **kw): super().__init__(message, code="payload_too_large", status_code=413, **kw)
class UnsupportedMediaType(AppError):
    def __init__(self, message="Unsupported media type.", **kw): super().__init__(message, code="unsupported_media_type", status_code=415, **kw)
class LengthRequired(AppError):
    def __init__(self, message="Content-Length is required.", **kw): super().__init__(message, code="length_required", status_code=411, **kw)
class NotFound(AppError):
    def __init__(self, message="Resource not found.", **kw): super().__init__(message, code="not_found", status_code=404, **kw)
class AuthenticationRequired(AppError):
    def __init__(self, message="A valid bearer token is required.", **kw): super().__init__(message, code="authentication_required", status_code=401, **kw)
class RateLimited(AppError):
    def __init__(self, message="Too many requests.", **kw): super().__init__(message, code="rate_limited", status_code=429, **kw)
class ServiceUnavailable(AppError):
    def __init__(self, message="Service unavailable.", *, code="service_unavailable", **kw): super().__init__(message, code=code, status_code=503, **kw)
