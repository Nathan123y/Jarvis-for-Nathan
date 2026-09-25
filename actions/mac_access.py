"""On-demand macOS permission diagnostics; never changes privacy settings."""
from __future__ import annotations

import ctypes
import os
import platform
import subprocess
from pathlib import Path


def _accessibility() -> str:
    try:
        framework = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        framework.AXIsProcessTrusted.restype = ctypes.c_bool
        if framework.AXIsProcessTrusted():
            return "Accessibility: allowed for the running Python process."
    except (OSError, AttributeError):
        return "Accessibility: could not check."
    return (
        "Accessibility: unavailable to the running Python process. Check "
        "System Settings → Privacy & Security → Accessibility for Jarvis and Python, "
        "then quit and reopen Jarvis."
    )


def _folder(label: str) -> str:
    folder = Path.home() / label
    try:
        # Opening the directory checks access without reading or returning names.
        with os.scandir(folder):
            pass
        return f"{label}: accessible."
    except FileNotFoundError:
        return f"{label}: folder not found."
    except PermissionError:
        return (
            f"{label}: access denied. Check System Settings → Privacy & Security → "
            f"Files & Folders → Jarvis (or Python) → {label}, then reopen Jarvis."
        )
    except OSError as exc:
        return f"{label}: access check failed ({type(exc).__name__})."


def _automation(app: str, noun: str) -> str:
    # Only invoke these scripts after an explicit diagnostic request: macOS may
    # display its own first-use Automation prompt. Return counts, never data.
    try:
        result = subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to count {noun}'],
            capture_output=True, text=True, timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{app}: did not respond to the access check."
    if result.returncode == 0:
        return f"{app}: Automation access worked."
    detail = (result.stderr or "").lower()
    if "-1743" in detail or "not authorized to send apple events" in detail:
        return (
            f"{app}: Automation denied. Check System Settings → Privacy & Security "
            f"→ Automation → Jarvis (or Python) → {app}, then reopen Jarvis."
        )
    return f"{app}: could not complete the check; open {app} and try again."


def mac_access(parameters: dict | None = None) -> str:
    if platform.system() != "Darwin":
        return "Mac permission checks are available only on macOS."
    scope = str((parameters or {}).get("scope", "all")).lower().strip()
    checks = {
        "device": [_accessibility],
        "files": [lambda name=name: _folder(name) for name in ("Desktop", "Documents", "Downloads")],
        "contacts": [lambda: _automation("Contacts", "people")],
        "calendar": [lambda: _automation("Calendar", "calendars")],
    }
    if scope not in (*checks, "all"):
        return "Choose device, files, contacts, calendar, or all."
    selected = checks.keys() if scope == "all" else (scope,)
    results = [check() for group in selected for check in checks[group]]
    return "Mac access check:\n" + "\n".join(results)


TOOL = {
    "name": "mac_access",
    "description": (
        "Check Jarvis's macOS access for device controls (Accessibility), local "
        "Desktop/Documents/Downloads files, Contacts, and Calendar. Use on "
        "request when a Mac control or data access fails or the user asks what "
        "permissions Jarvis has. Read-only; Contacts/Calendar checks may show "
        "the normal macOS Automation permission prompt. Does not grant access."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"scope": {
            "type": "STRING", "description": "device, files, contacts, calendar, or all (default)."
        }},
    },
    "handler": mac_access,
}
