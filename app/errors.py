"""Error envelope: { "error": { "code", "message", "details" } } (architecture §4)."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 500
    code = "INTERNAL"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class UnsupportedType(AppError):
    status_code, code = 415, "UNSUPPORTED_TYPE"


class ImageTooLarge(AppError):
    status_code, code = 413, "IMAGE_TOO_LARGE"


class ImageTooSmall(AppError):
    status_code, code = 400, "IMAGE_TOO_SMALL"


class DecodeFailed(AppError):
    status_code, code = 400, "DECODE_FAILED"


class ConsentRequired(AppError):
    status_code, code = 400, "CONSENT_REQUIRED"


class QueryCapReached(AppError):
    status_code, code = 429, "QUERY_CAP_REACHED"


class RateLimited(AppError):
    status_code, code = 429, "RATE_LIMITED"


class NotFound(AppError):
    status_code, code = 404, "NOT_FOUND"


class NotReady(AppError):
    status_code, code = 409, "NOT_READY"


class ProviderUnavailable(AppError):
    status_code, code = 503, "PROVIDER_UNAVAILABLE"


class PhotoUnprocessable(AppError):
    """E14: vision-provider moderation flag on the query image. Generic copy, logged."""

    status_code, code = 422, "PHOTO_UNPROCESSABLE"


class BadRequest(AppError):
    status_code, code = 400, "BAD_REQUEST"


class ProviderConfigError(AppError):
    """Selected provider has no usable credentials/deps. Fail loudly at boot."""

    status_code, code = 500, "PROVIDER_MISCONFIGURED"
