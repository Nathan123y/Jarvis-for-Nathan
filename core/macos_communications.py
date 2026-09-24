"""Native macOS contact, Messages, Phone, and FaceTime helpers.

All user-supplied values are passed to ``osascript`` as argv.  They are never
interpolated into AppleScript source, which prevents names or message text from
turning into executable script.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from urllib.parse import quote


_DIRECT_PHONE = re.compile(r"^\+?[0-9(). -]{3,}$")
_DIRECT_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMERGENCY_NUMBERS = {"000", "110", "112", "119", "911", "999"}
_CONTACT_TIMEOUT = 8


class ContactError(RuntimeError):
    pass


@dataclass(frozen=True)
class Contact:
    name: str
    handle: str

    @property
    def masked_handle(self) -> str:
        if "@" in self.handle:
            local, domain = self.handle.split("@", 1)
            return f"{local[:2]}***@{domain}"
        digits = re.sub(r"\D", "", self.handle)
        return f"••• ••• {digits[-4:]}" if len(digits) >= 4 else self.handle


_CONTACT_SCRIPT = r'''
on run argv
    set wanted to item 1 of argv
    set handleKind to item 2 of argv
    tell application "Contacts"
        set matches to every person whose name is wanted
        if (count of matches) is 0 then
            try
                set matches to every person whose nickname is wanted
            end try
        end if
        if (count of matches) is 0 then
            set matches to every person whose name contains wanted
        end if
        if (count of matches) is 0 then
            try
                set matches to every person whose nickname contains wanted
            end try
        end if
        if (count of matches) is 0 then
            set matches to every person whose first name is wanted
        end if
        if (count of matches) is 0 then
            set matches to every person whose last name is wanted
        end if
        if (count of matches) is 0 then return "NOT_FOUND"
        if (count of matches) > 1 then
            set foundNames to {}
            repeat with p in matches
                set end of foundNames to (name of p as text)
            end repeat
            set AppleScript's text item delimiters to " | "
            return "AMBIGUOUS" & tab & (foundNames as text)
        end if
        set p to item 1 of matches
        set phoneItems to phones of p
        if (count of phoneItems) > 0 then
            if (count of phoneItems) is 1 then
                set chosen to value of item 1 of phoneItems
            else
                set mobileItems to {}
                repeat with ph in phoneItems
                    set phoneLabel to label of ph as text
                    ignoring case
                        if phoneLabel contains "mobile" or phoneLabel contains "iPhone" or phoneLabel contains "cell" then
                            set end of mobileItems to ph
                        end if
                    end ignoring
                end repeat
                if (count of mobileItems) is 1 then
                    set chosen to value of item 1 of mobileItems
                else
                    set uniqueNumbers to {}
                    repeat with ph in phoneItems
                        set phoneNumber to value of ph as text
                        if uniqueNumbers does not contain phoneNumber then set end of uniqueNumbers to phoneNumber
                    end repeat
                    if (count of uniqueNumbers) is 1 then
                        set chosen to item 1 of uniqueNumbers
                    else
                        return "MULTIPLE_HANDLES" & tab & (name of p as text)
                    end if
                end if
            end if
        else if handleKind is not "phone" and (count of emails of p) is 1 then
            set chosen to value of item 1 of emails of p
        else if handleKind is not "phone" and (count of emails of p) > 1 then
            return "MULTIPLE_HANDLES" & tab & (name of p as text)
        else
            return "NO_HANDLE" & tab & (name of p as text)
        end if
        return "OK" & tab & (name of p as text) & tab & (chosen as text)
    end tell
end run
'''


_MESSAGE_SCRIPT = r'''
on run argv
    set recipientHandle to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        set usableServices to every service whose service type is iMessage
        if (count of usableServices) is 0 then error "No iMessage service is signed in."
        set targetService to item 1 of usableServices
        set targetBuddy to buddy recipientHandle of targetService
        send messageBody to targetBuddy
    end tell
    return "OK"
end run
'''


def _osascript(script: str, *args: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContactError("Contacts or Messages took too long to respond. Try again in a moment.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "macOS automation failed").strip()
        if "-1743" in detail or "not authorized to send Apple events" in detail.lower():
            raise ContactError(
                "macOS denied automation access to Contacts or Messages. Allow Jarvis "
                "(or Python, if that is what macOS lists) under System Settings → "
                "Privacy & Security → Automation, then reopen Jarvis."
            )
        raise RuntimeError(detail)
    return result.stdout.strip()


def normalize_phone(value: str) -> str:
    value = value.strip()
    prefix = "+" if value.startswith("+") else ""
    return prefix + re.sub(r"\D", "", value)


def is_emergency_number(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return digits in _EMERGENCY_NUMBERS


def resolve_contact(query: str, *, phone_only: bool = False) -> Contact:
    """Resolve a direct handle or an unambiguous macOS Contacts name."""
    query = re.sub(r"^(?:(?:my|the)\s+)+", "", query.strip(), flags=re.IGNORECASE).strip()
    if not query:
        raise ContactError("Please specify a contact name or phone number.")
    if _DIRECT_PHONE.fullmatch(query):
        return Contact(name=query, handle=normalize_phone(query))
    if not phone_only and _DIRECT_EMAIL.fullmatch(query):
        return Contact(name=query, handle=query)

    raw = _osascript(_CONTACT_SCRIPT, query, "phone" if phone_only else "message", timeout=_CONTACT_TIMEOUT)
    parts = raw.split("\t")
    status = parts[0] if parts else ""
    if status == "OK" and len(parts) >= 3:
        return Contact(name=parts[1], handle=parts[2])
    if status == "AMBIGUOUS":
        choices = parts[1] if len(parts) > 1 else "multiple contacts"
        raise ContactError(f"More than one contact matches '{query}': {choices}. Please use the full name.")
    if status == "NO_HANDLE":
        name = parts[1] if len(parts) > 1 else query
        kind = "phone number" if phone_only else "phone number or email"
        raise ContactError(f"{name} has no {kind} in Contacts.")
    if status == "MULTIPLE_HANDLES":
        name = parts[1] if len(parts) > 1 else query
        raise ContactError(
            f"{name} has multiple possible numbers. Say the exact phone number so I do not choose the wrong one."
        )
    raise ContactError(f"I could not find '{query}' in Contacts. Say the full contact name or a number.")


def send_message(contact: Contact, message: str) -> str:
    if not message.strip():
        raise ValueError("Message text cannot be empty.")
    _osascript(_MESSAGE_SCRIPT, contact.handle, message)
    return f"Message sent to {contact.name}."


def start_call(contact: Contact, *, facetime_audio: bool = False) -> str:
    if is_emergency_number(contact.handle):
        raise ValueError("Jarvis cannot place emergency calls. Dial emergency services yourself.")
    scheme = "facetime-audio" if facetime_audio else "tel"
    url = f"{scheme}://{quote(contact.handle, safe='+@')}"
    result = subprocess.run(
        ["open", url], capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "could not open the calling app").strip()
        raise RuntimeError(detail)
    kind = "FaceTime Audio" if facetime_audio else "phone"
    return f"Started a {kind} call to {contact.name}."
