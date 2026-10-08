"""Shared network settings for the campaign's own web requests (business search, site audits, preview checks).

The python.org build of Python on a Mac ships without root certificates until someone runs its
"Install Certificates" step, so every https request fails with a bare URLError. certifi (already installed
with the Gemini library) has the certificates, so use it when it is there."""
from __future__ import annotations

import ssl
from typing import Optional

_ctx: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    global _ctx
    if _ctx is None:
        try:
            import certifi
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
    return _ctx


def why(exc: BaseException) -> str:
    """A short, readable reason for a failed request (URLError hides it in .reason)."""
    r = getattr(exc, "reason", None) or exc
    if isinstance(r, str):
        return r[:120]
    return f"{type(r).__name__}: {str(r)[:120]}"
