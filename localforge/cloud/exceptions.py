"""Custom exceptions for the cloud module."""

from __future__ import annotations


class CloudError(Exception):
    """Base exception for all cloud module errors."""


class AuthExpiredError(CloudError):
    """Raised when the API returns 401/403 indicating expired session cookies."""

    def __init__(self, message: str = "Session expired. Please paste fresh request headers from your browser.") -> None:
        super().__init__(message)


class VPNError(CloudError):
    """Raised when the API endpoint is unreachable (likely VPN not connected)."""

    def __init__(self, message: str = "Cannot reach the API. Are you connected to the VPN?") -> None:
        super().__init__(message)


class APIError(CloudError):
    """Raised on unexpected server-side errors (5xx, malformed responses, etc.)."""

    def __init__(self, status_code: int = 0, message: str = "Unexpected API error.") -> None:
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}" if status_code else message)


class RateLimitError(CloudError):
    """Raised when the API returns 429 — too many requests."""

    def __init__(self, retry_after: float = 30.0) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after:.0f}s.")
