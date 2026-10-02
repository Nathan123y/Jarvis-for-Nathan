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
