"""Record where Jarvis was when Python itself crashes.

A hard crash ("Python quit unexpectedly") skips every try/except, so normal
logging never sees it. faulthandler writes the Python stack of every thread
to a file at the moment of the crash, which is what pins down the cause.
The file holds only code locations (file names, line numbers, function
names) — no conversation text, keys or notes.
"""
from __future__ import annotations

import faulthandler
import os
import platform
import time
from pathlib import Path

_MAX_BYTES = 512_000
_handle = None


def crash_log_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "Jarvis" / "crash.log"
    return Path.home() / ".config" / "jarvis" / "crash.log"


def enable(path: Path | None = None) -> Path | None:
    """Turn on crash stacks. Safe to call more than once; never raises."""
    global _handle
    try:
        path = path or crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_suffix(".old.log"))
        if _handle is not None:
            return path
        _handle = open(path, "a", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _handle.write(f"\n=== Jarvis started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"pid {os.getpid()} ===\n")
        _handle.flush()
        faulthandler.enable(file=_handle, all_threads=True)
        return path
    except Exception:
        return None
