"""Small, dependency-free helpers for Gemini Live session rotation."""
from __future__ import annotations

import re


def seconds_until_go_away(value: object) -> float:
    """Accept the SDK's duration string (e.g. '30s') or a timedelta."""
    try:
        if hasattr(value, "total_seconds"):
            return max(0.0, float(value.total_seconds()))
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value))
        if match:
            return float(match.group(1))
    except (TypeError, ValueError, OverflowError):
        pass
    return 10.0  # A missing duration should still rotate promptly.


def exception_details(exc: BaseException) -> str:
    """Unwrap TaskGroup errors so the retry policy sees the actual server code."""
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(exception_details(child) for child in exc.exceptions)
    return str(exc)

def is_expected_session_expiry(details: str, connection_age: float) -> bool:
    """Recognize a GoAway close, including the bare abort seen on older previews."""
    lower = details.lower()
    return ("goaway" in lower or
            ("1008" in lower and "operation was aborted" in lower
             and connection_age >= 120.0))


def leaf_exception_name(exc: BaseException) -> str:
    """Type name of the first real error inside a TaskGroup's exception group."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return type(exc).__name__


_LABEL_UNSAFE = re.compile(r"[^a-z0-9_:-]+")
_NETWORK_HINTS = ("timeouterror", "timed out", "getaddrinfo", "connectionrefused",
                  "oserror", "cannot connect", "connection reset", "network")


def safe_label(text: str, limit: int = 32) -> str:
    """Reduce an internal reason string to a short, log-safe token."""
    return _LABEL_UNSAFE.sub("-", str(text or "").strip().lower())[:limit].strip("-")


def classify_disconnect(exc_name: str, details: str, resumed_with_handle: bool,
                        expected_expiry: bool) -> str:
    """Name why a live session ended, for the performance trace.

    Returns a category, never the error text: server messages can echo request
    details, and the trace is meant to be safe to attach to a bug report.
    """
    lower = (details or "").lower()
    if expected_expiry:
        return "server_expiry"
    if "api key not valid" in lower or "1007" in lower:
        return "api_key"
    if resumed_with_handle and ("resum" in lower or "handle" in lower
                                or "invalid_argument" in lower or "not_found" in lower):
        return "resume_rejected"
    if any(hint in lower for hint in _NETWORK_HINTS):
        return "network"
    return "error:" + safe_label(exc_name)
