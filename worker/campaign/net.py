"""Shared network settings for the campaign's own web requests (business search, site audits, preview checks).

The python.org build of Python on a Mac ships without root certificates until someone runs its
"Install Certificates" step, so every https request fails with a bare URLError. certifi (already installed
with the Gemini library) has the certificates, so use it when it is there."""
from __future__ import annotations

import socket
import ssl
from typing import Callable, Optional

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


def email_domain_problem(email: str, resolve: Callable = socket.getaddrinfo) -> str:
    """"" when the address's domain exists, "missing" when the name does not exist at all (the mail would
    bounce, and bounces stop the campaign), "unknown" when the lookup itself failed (not a verdict)."""
    domain = (email or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return "missing"
    try:
        return "" if resolve(domain, None) else "missing"
    except socket.gaierror as exc:
        return "missing" if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)) else "unknown"
    except OSError:
        return "unknown"
