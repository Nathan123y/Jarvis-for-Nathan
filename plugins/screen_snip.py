"""Screen snips and the clipboard: grab part of the screen, copy it, send it.

- snip:      the user drags a box over the part of the screen they want (or
             clicks a window, or takes the whole screen). The picture is saved
             in ~/Pictures/Jarvis Snips and put on the clipboard, ready to paste.
- copy:      put any text on the clipboard.
- copy_text: read the words in the last snip (macOS text recognition, on this
             Mac) and put them on the clipboard as plain text.
- paste:     press Cmd+V in the app in front.
- send:      send the last snip to a person or a named group chat in iMessage,
             or as a Gmail attachment. Both wait for the on-screen CONFIRM.

Snips live in ~/Pictures because Messages, which is sandboxed, will not send a
file it is given from most other folders when asked through AppleScript.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from core import confirm

PLUGIN = {
    "name": "screen_snip",
    "description": (
        "Screenshots of part of the screen, and the clipboard. Actions: snip (the user "
        "drags a box over what they want; mode=window to click a window, mode=full for "
        "the whole screen; the snip goes on the clipboard so they can paste it), "
        "copy (put text on the clipboard: 'copy that', 'put that on my clipboard', "
        "'copy the address'; pass the exact text), copy_text (copy the words that are "
        "in the last snip: 'copy the text from that', 'grab the text off my screen' — "
        "if they have not snipped yet, call snip first), paste (press Cmd+V in the front "
        "app), send (send the last snip: via=imessage with to=a contact name, number "
        "or group chat name; via=email with to=an exact email address and optionally "
        "account=personal|school|spam and subject). For 'screenshot this and send it to "
        "<someone>', call snip first, then send. Sending waits for the user's yes or the "
        "on-screen CONFIRM; do not call send again after they agree."
    ),
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["snip", "copy", "copy_text", "paste", "send"]},
            "mode": {"type": "STRING", "enum": ["area", "window", "full"],
                     "description": "For snip; default area (drag a box)"},
            "text": {"type": "STRING", "description": "For copy: the exact text to put on the clipboard"},
            "via": {"type": "STRING", "enum": ["imessage", "email"], "description": "For send"},
            "to": {"type": "STRING", "description": "For send: contact, number, group chat name, or email address"},
            "message": {"type": "STRING", "description": "For send: optional text to go with the snip"},
            "subject": {"type": "STRING", "description": "For send by email: subject (default 'Screenshot')"},
            "account": {"type": "STRING", "description": "For send by email: personal, school or spam Gmail"},
        },
        "required": ["action"],
    },
}

SNIP_DIR = Path.home() / "Pictures" / "Jarvis Snips"
_KEEP = 200               # older snips are deleted past this many
_SNIP_TIMEOUT = 120       # seconds the user has to drag the box
_OCR_TIMEOUT = 90         # the first recognition after a restart loads the models (~50 s)
_MAX_COPY = 100_000

_lock = threading.Lock()
_last: dict = {"path": None}

_FILLER = {"the", "my", "group", "groupchat", "gc", "chat", "in", "imessage", "messages", "to"}

_CLIPBOARD_IMAGE = '''
on run argv
    set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)
end run
'''

_OCR_SCRIPT = '''
use framework "Foundation"
use framework "Vision"
use scripting additions
on run argv
    set imgURL to current application's NSURL's fileURLWithPath:(item 1 of argv)
    set reqHandler to current application's VNImageRequestHandler's alloc()'s initWithURL:imgURL options:(current application's NSDictionary's dictionary())
    set req to current application's VNRecognizeTextRequest's alloc()'s init()
    req's setRecognitionLevel:0
    req's setUsesLanguageCorrection:true
    set {ok, err} to reqHandler's performRequests:{req} |error|:(reference)
    if not (ok as boolean) then error (err's localizedDescription() as text)
    set out to {}
    repeat with obs in (req's results() as list)
        set end of out to ((obs's topCandidates:1)'s firstObject()'s |string|()) as text
    end repeat
    set AppleScript's text item delimiters to linefeed
    return out as text
end run
'''

# kind is "chat" (a group, by its chat.db identifier) or "buddy" (a handle).
_SEND_SCRIPT = '''
on run argv
    set sendKind to item 1 of argv
    set targetId to item 2 of argv
    set snipFile to POSIX file (item 3 of argv)
    set noteText to item 4 of argv
    tell application "Messages"
        if sendKind is "chat" then
            set destination to missing value
            repeat with c in chats
                if (id of c as text) ends with (";" & targetId) then
                    set destination to c
                    exit repeat
                end if
            end repeat
            if destination is missing value then error "Messages has no open chat for that group."
        else
            set usableServices to every service whose service type is iMessage
            if (count of usableServices) is 0 then error "No iMessage service is signed in."
            set destination to buddy targetId of (item 1 of usableServices)
        end if
        if noteText is not "" then send noteText to destination
        send snipFile to destination
    end tell
    return "OK"
end run
'''


def _osascript(script: str, *args: str, timeout: int = 20) -> str:
    result = subprocess.run(["osascript", "-", *args], input=script,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "macOS automation failed").strip())
    return result.stdout.strip()


def _words(value) -> set[str]:
    return set("".join(c if c.isalnum() else " " for c in str(value or "").lower()).split())


def _prune() -> None:
    snips = sorted(SNIP_DIR.glob("snip-*.png"))
    for old in snips[:-_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def _last_snip() -> Path | None:
    with _lock:
        path = _last["path"]
    return path if path and path.exists() else None


def _snip(mode: str) -> str:
    SNIP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNIP_DIR / time.strftime("snip-%Y%m%d-%H%M%S.png")
    flags = {"area": ["-i", "-s"], "window": ["-i", "-W"], "full": []}.get(mode, ["-i", "-s"])
    try:
        subprocess.run(["screencapture", "-x", *flags, str(path)], timeout=_SNIP_TIMEOUT,
                       capture_output=True)
    except subprocess.TimeoutExpired:
        return "The snip timed out before you picked an area, so nothing was taken."
    # Escape during the drag leaves no file; a missing Screen Recording grant
    # leaves an empty one or none.
    if not path.exists() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        return ("No snip was taken — it was cancelled, or macOS is blocking screen capture "
                "(System Settings → Privacy & Security → Screen Recording).")
    with _lock:
        _last["path"] = path
    _prune()
    try:
        _osascript(_CLIPBOARD_IMAGE, str(path))
    except Exception:
        return f"Snip saved as {path.name} in Pictures/Jarvis Snips, but I couldn't put it on the clipboard."
    return "Got it — the snip is on your clipboard, ready to paste, and saved in Pictures/Jarvis Snips."


def _copy(text: str) -> str:
    if not text.strip():
        return "What should I copy? Tell me the text."
    if len(text) > _MAX_COPY:
        return "That is too long to copy in one go."
    subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
    words = len(text.split())
    return f"Copied to your clipboard ({words} word{'s' if words != 1 else ''}). Paste it with Cmd+V."


def _copy_text() -> str:
    path = _last_snip()
    if path is None:
        return "There's no snip to read yet. Ask me to snip the part of the screen with the text first."
    try:
        text = _osascript(_OCR_SCRIPT, str(path), timeout=_OCR_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "Reading the text took too long. Try again — the second try is much faster."
    if not text.strip():
        return "I couldn't find any text in that snip."
    subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
    preview = text if len(text) <= 300 else text[:297] + "…"
    return f"Copied the text from the snip to your clipboard. It reads:\n{preview}"


def _paste() -> str:
    _osascript('tell application "System Events" to keystroke "v" using command down')
    return "Pasted."


def _find_group(name: str) -> tuple[str, str] | None:
    """(chat identifier, title) of the named group chat the user means, if one fits."""
    want = _words(name) - _FILLER
    if not want:
        return None
    from core import imessage
    groups = imessage.named_groups()
    exact = [g for g in groups if _words(g[1]) - _FILLER == want]
    if exact:
        return exact[0]
    loose = [g for g in groups if want <= _words(g[1])]
    if len(loose) > 1:
        names = ", ".join(sorted({g[1] for g in loose})[:5])
        raise ValueError(f"More than one group chat matches '{name}': {names}. Which one?")
    return loose[0] if loose else None


def _send_imessage(to: str, note: str, path: Path) -> str:
    from core.macos_communications import ContactError, resolve_contact
    try:
        group = _find_group(to)
    except ValueError as exc:
        return str(exc)
    except Exception:
        group = None   # chat.db unreadable: fall back to a contact
    if group:
        chat_id, label = group
        args = ("chat", chat_id)
        title = f"SEND SNIP TO GROUP {label.upper()}?"
        detail = f"To group chat: {label}"
    else:
        try:
            contact = resolve_contact(to)
        except ContactError as exc:
            return f"{exc} If it's a group chat, say its name exactly as it shows in Messages."
        label = contact.name
        args = ("buddy", contact.handle)
        title = f"SEND SNIP TO {label.upper()}?"
        detail = f"To: {label} ({contact.masked_handle})"
    if note:
        detail += f"\n\n{note[:200]}"
    detail += f"\n\nAttachment: {path.name}"

    def run() -> str:
        _osascript(_SEND_SCRIPT, *args, str(path), note, timeout=30)
        return f"Snip sent to {label} in Messages."

    return confirm.request(key=f"snip:{args[1]}", title=title, detail=detail, run=run)


def _send_email(to: str, note: str, subject: str, account: str, player, path: Path) -> str:
    from plugins import gmail
    to = gmail._email_address(to)
    subject = (subject or "Screenshot").strip()
    if "\n" in subject or "\r" in subject or len(subject) > 200:
        return "Please give me a subject on one line (up to 200 characters)."
    account = gmail._choose_account("send", account)
    service = gmail._service(account)
    from_address = gmail._email_address(
        service.users().getProfile(userId="me").execute().get("emailAddress", ""))
    body = note or "Screenshot attached."
    if player:
        player.show_content(
            f"{account.upper()} GMAIL — SNIP TO SEND",
            f"From: {from_address}\nTo: {to}\nSubject: {subject}\nAttachment: {path.name}\n\n{body}",
        )
    return confirm.request(
        key=f"snip-email:{to}",
        title=f"EMAIL SNIP FROM {account.upper()} GMAIL TO {gmail._display(to, 45)}?",
        detail=f"From: {from_address}\nTo: {to}\nSubject: {subject}\nAttachment: {path.name}\n\n{body[:200]}",
        run=lambda: gmail._send(service, to, subject, body, from_address, attachment=path),
    )


def _send(args: dict, player) -> str:
    path = _last_snip()
    if path is None:
        return "There's no snip to send yet. Ask me to snip the part of the screen you want first."
    to = str(args.get("to") or "").strip()
    if not to:
        return "Who should I send the snip to?"
    if confirm.pending_title():
        return "A confirmation is already on screen. Answer that one first."
    note = str(args.get("message") or "").strip()
    via = str(args.get("via") or "").strip().lower()
    if via not in {"imessage", "email"}:
        via = "email" if "@" in to else "imessage"
    if via == "email":
        return _send_email(to, note, str(args.get("subject") or ""),
                           str(args.get("account") or ""), player, path)
    return _send_imessage(to, note, path)


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "snip":
            result = _snip(str(args.get("mode") or "area").strip().lower())
        elif action == "copy":
            result = _copy(str(args.get("text") or ""))
        elif action == "copy_text":
            result = _copy_text()
        elif action == "paste":
            result = _paste()
        elif action == "send":
            result = _send(args, player)
        else:
            return "Screen snip actions: snip, copy, copy_text, paste, or send."
    except (RuntimeError, ValueError) as exc:
        result = f"That didn't work: {exc}"
    except ImportError:
        return "Gmail dependencies are missing. Run python3 setup.py in the Jarvis folder."
    except Exception as exc:
        return f"Screen snip {action} failed ({type(exc).__name__}: {str(exc)[:120]})."
    if player and not result.startswith("[CONFIRMATION_PENDING]"):
        try:
            player.write_log(f"JARVIS: {result.splitlines()[0]}")
        except Exception:
            pass
    return result
