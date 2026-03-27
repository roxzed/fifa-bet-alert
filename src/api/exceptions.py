"""Custom exceptions for BetsAPI client."""


class BetsAPIError(Exception):
    """Base exception for all BetsAPI errors.

    Attributes:
        message: Human-readable error description.
        status_code: HTTP status code if available.
        response_body: Raw response body if available.
    """

    def __init__(
        self,
        message: str = "BetsAPI error",
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " | ".join(parts)


class BetsAPIAuthError(BetsAPIError):
    """Authentication/authorization error (HTTP 401 or 403).

    Raised when the API token is invalid, expired, or lacks
    permission for the requested resource.
    """

    def __init__(
        self,
        message: str = "Authentication failed - check your BetsAPI token",
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message, status_code, response_body)


class BetsAPIRateLimitError(BetsAPIError):
    """Rate limit exceeded (HTTP 429).

    The client should wait before making additional requests.

    Attributes:
        retry_after: Seconds to wait before retrying, if provided by the API.
    """

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        status_code: int | None = 429,
        response_body: str | None = None,
        retry_after: float | None = None,
    ):
        self.retry_after = retry_after
        super().__init__(message, status_code, response_body)


class BetsAPINotFoundError(BetsAPIError):
    """Resource not found (HTTP 404).

    The requested event, league, or resource does not exist.
    """

    def __init__(
        self,
        message: str = "Resource not found",
        status_code: int | None = 404,
        response_body: str | None = None,
    ):
        super().__init__(message, status_code, response_body)


class BetsAPITimeoutError(BetsAPIError):
    """Request timeout.

    The API did not respond within the configured timeout period.
    """

    def __init__(
        self,
        message: str = "Request timed out",
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message, status_code, response_body)
