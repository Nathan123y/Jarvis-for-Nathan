"""Confirmed macOS Phone and FaceTime Audio calls."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from core import confirm
from core.macos_communications import (
    ContactError,
    is_emergency_number,
    resolve_contact,
    start_call,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _is_mac() -> bool:
    try:
        data = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return data.get("os_system", "").lower() == "mac"
    except Exception:
        return sys.platform == "darwin"


def phone_call(parameters: dict, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    recipient = str(params.get("recipient") or params.get("contact") or "").strip()
    mode = str(params.get("mode") or "phone").lower().strip().replace("-", "_").replace(" ", "_")
    facetime_audio = mode in {"facetime", "facetime_audio", "audio_facetime"}

    if not _is_mac():
        return "Phone and FaceTime calling is currently available only on macOS."
    if not recipient:
        return "Please specify who to call."

    try:
        contact = resolve_contact(recipient, phone_only=True)
    except ContactError as e:
        return str(e)

    if is_emergency_number(contact.handle):
        return ("Jarvis cannot place emergency calls. "
                "Please dial emergency services yourself.")
    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen. "
                "Answer that one before starting a call.")

    call_kind = "FACETIME AUDIO" if facetime_audio else "PHONE"
    return confirm.request(
        key=f"call:{mode}:{contact.handle}",
        title=f"START {call_kind} CALL?",
        detail=f"Call {contact.name} ({contact.masked_handle})",
        run=lambda c=contact, ft=facetime_audio: start_call(c, facetime_audio=ft),
    )


TOOL = {
    "name": "phone_call",
    "description": (
        "Starts a phone or FaceTime Audio call on macOS. Resolve the person from "
        "Contacts and call this tool once. The call never starts until the user "
        "presses CONFIRM on the HUD. Never claim the call started while confirmation "
        "is pending. Emergency calls are refused."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "recipient": {
                "type": "STRING",
                "description": "Exact contact name or phone number to call",
            },
            "mode": {
                "type": "STRING",
                "description": "phone (default) or facetime_audio",
            },
        },
        "required": ["recipient"],
    },
    "handler": phone_call,
}
