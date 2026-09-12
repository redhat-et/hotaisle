"""Exceptions raised by the Hot Aisle client."""

from __future__ import annotations


class HotAisleError(Exception):
    """Base class for all errors raised by this module."""


class ConfigurationError(HotAisleError):
    """The client is not configured correctly (e.g. no API key, no team)."""


class AuthError(HotAisleError):
    """The API rejected our credentials (HTTP 401/403)."""

    def __init__(self, message: str, status_code: int = 401, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class APIError(HotAisleError):
    """The API returned a non-success status code."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        body: str = "",
        method: str = "",
        path: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.method = method
        self.path = path


class NotFoundError(APIError):
    """HTTP 404 — resource (team, VM, server) does not exist or is not yours."""


class ValidationError(APIError):
    """HTTP 400/422 — the request body or parameters were rejected."""


class InsufficientBalanceError(APIError):
    """HTTP 402 — the team cannot pay for the requested resource."""


class PreconditionFailedError(APIError):
    """HTTP 428 — team has no accepted user-role member with an SSH key."""


_STATUS_MAP = {
    400: ValidationError,
    402: InsufficientBalanceError,
    404: NotFoundError,
    422: ValidationError,
    428: PreconditionFailedError,
}


def error_for_status(
    status_code: int, body: str, method: str = "", path: str = ""
) -> APIError:
    """Build the most specific APIError subclass for an HTTP status code."""
    cls = _STATUS_MAP.get(status_code, APIError)
    snippet = (body or "").strip()
    message = "HTTP %d from %s %s" % (status_code, method, path)
    if snippet:
        message = "%s: %s" % (message, snippet[:500])
    return cls(message, status_code=status_code, body=body, method=method, path=path)
