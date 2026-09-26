from __future__ import annotations

from typing import Any


class TRError(Exception):
    """Base class of every error raised by trclient."""


class AuthError(TRError):
    pass


class LoginError(AuthError):
    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class LoginTimeout(LoginError):
    pass


class NotLoggedIn(AuthError):
    pass


class SessionExpired(AuthError):
    """The refresh token is no longer accepted - a new login is needed."""


class StoreUnavailable(TRError):
    pass


class APIError(TRError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class TRConnectionError(TRError):
    pass


class ConnectionLost(TRConnectionError):
    """The websocket closed while a subscription was open."""


class SubscriptionClosed(TRError):
    pass


class SubscriptionError(TRError):
    def __init__(self, sub_id: str, payload: dict, error: Any):
        self.sub_id = sub_id
        self.payload = payload
        self.error = error
        super().__init__(f"subscription {payload.get('type')!r} failed: {self.codes or error}")

    @property
    def codes(self) -> list[str]:
        err = self.error
        if isinstance(err, dict):
            if isinstance(err.get("errors"), list):
                return [e.get("errorCode") for e in err["errors"] if isinstance(e, dict) and e.get("errorCode")]
            if err.get("errorCode"):
                return [err["errorCode"]]
        return []

    @property
    def is_auth_error(self) -> bool:
        return "AUTHENTICATION_ERROR" in self.codes or "UNAUTHORIZED" in self.codes


class OrderValidationError(TRError, ValueError):
    pass


class GuardrailViolation(TRError):
    pass


class OrderRejected(TRError):
    def __init__(self, message: str, *, code: str | None = None, response: Any = None):
        super().__init__(message)
        self.code = code
        self.response = response


class OrderStateUnknown(TRError):
    """The order was sent but no answer arrived. It MAY have been placed - check orders() before retrying."""

    def __init__(self, message: str, *, client_process_id: str):
        super().__init__(message)
        self.client_process_id = client_process_id
