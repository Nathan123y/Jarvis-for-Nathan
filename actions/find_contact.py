"""Read-only lookup of a person in the Mac Contacts app."""
from __future__ import annotations

import platform
import re

from core.macos_communications import ContactError, resolve_contact


def find_contact(parameters: dict | None = None) -> str:
    if platform.system() != "Darwin":
        return "Contacts lookup is available only on macOS."
    name = str((parameters or {}).get("name") or "").strip()
    if not name:
        return "Tell me the name of the person to look up in Contacts."
    if "@" in name or re.fullmatch(r"\+?[\d(). -]{3,}", name):
        return "Give me the person's name to check whether they are in Contacts."
    try:
        contact = resolve_contact(name)
    except ContactError as exc:
        return str(exc)
    except (OSError, RuntimeError):
        return "Contacts could not be searched just now. Open Contacts and try again."
    # A name lookup must never start a call or send a message. Keep the number
    # masked in the AI response while confirming which card was found.
    return f"Found {contact.name} in Contacts ({contact.masked_handle}). No message or call was started."


TOOL = {
    "name": "find_contact",
    "description": (
        "Find a person by name in the Mac Contacts app without calling or "
        "messaging them. Use for 'find [name] in my Contacts', 'do you have "
        "[name] in Contacts', or 'look up [name]'. Ask for the full name if "
        "there are multiple matches. This tool is read-only."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "The person's name to find in Contacts."},
        },
        "required": ["name"],
    },
    "handler": find_contact,
}
