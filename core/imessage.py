"""Read the Mac's Messages history (iMessage and SMS) — read-only.

Messages keeps everything in ~/Library/Messages/chat.db. macOS protects that
file: the app running Jarvis (Jarvis.app, or VS Code/Terminal when started
from there) needs Full Disk Access, granted once in System Settings → Privacy
& Security → Full Disk Access. Nothing is ever written to it.

Names come from the Contacts database (also read-only, same permission), so a
reply reads as "Mom" instead of "+1 408 555 0100".
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
_APPLE_EPOCH = 978307200          # 2001-01-01 in Unix time

NEEDS_ACCESS = (
    "I can't read Messages yet: macOS needs to give Jarvis Full Disk Access. Open System "
    "Settings → Privacy & Security → Full Disk Access, turn on Jarvis (if you start Jarvis "
    "from VS Code or Terminal, turn on that app instead), then quit and reopen Jarvis."
)


class AccessError(RuntimeError):
    pass


@dataclass
class Text:
    rowid: int
    handle: str          # phone number or email, "" for your own messages
    name: str            # contact name if known, else the handle
    text: str
    at: float            # Unix time
    from_me: bool
    chat: str            # group name, or "" for one-to-one
    service: str         # iMessage / SMS / RCS


# ── decoding ─────────────────────────────────────────────────────────────────
def decode_attributed_body(blob) -> str:
    """Plain text from Messages' `attributedBody` (an archived NSAttributedString).

    Newer macOS often leaves `message.text` empty and stores the words only
    here. The string follows the NSString class marker and a '+' byte, with a
    typedstream length: one byte, or 0x81 + 2 bytes, or 0x82 + 4 bytes."""
    if not blob:
        return ""
    data = bytes(blob)
    i = data.find(b"NSString")
    if i < 0:
        return ""
    j = data.find(b"+", i + 8)
    if j < 0 or j + 1 >= len(data):
        return ""
    n, pos = data[j + 1], j + 2
    if n == 0x81:
        n, pos = int.from_bytes(data[pos:pos + 2], "little"), pos + 2
    elif n == 0x82:
        n, pos = int.from_bytes(data[pos:pos + 4], "little"), pos + 4
    return data[pos:pos + n].decode("utf-8", "replace")


def apple_time(value) -> float:
    """Messages stores seconds (old) or nanoseconds (new) since 2001."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if v > 1e12:
        v /= 1e9
    return v + _APPLE_EPOCH if v else 0.0


def _clean(text: str, limit: int = 600) -> str:
    text = (text or "").replace("￼", "[attachment]").replace("​", "")
    return " ".join(text.split())[:limit]


def phone_key(value: str) -> str:
    """Comparable key for a phone number or email: last 10 digits, or lowercase email."""
    v = (value or "").strip().lower()
    if "@" in v:
        return v
    digits = re.sub(r"\D", "", v)
    return digits[-10:] if digits else v


# ── contacts ─────────────────────────────────────────────────────────────────
_names: dict[str, str] = {}
_names_at = 0.0


def contact_names(force: bool = False) -> dict[str, str]:
    """phone_key → display name, from the Contacts databases. Cached 10 minutes;
    empty if Contacts can't be read (names then fall back to the number)."""
    global _names, _names_at
    if not force and time.time() - _names_at < 600:
        return _names                       # cached, even when empty (no access)
    found: dict[str, str] = {}
    base = Path.home() / "Library" / "Application Support" / "AddressBook"
    paths = glob.glob(str(base / "Sources" / "*" / "AddressBook-v22.abcddb"))
    paths.append(str(base / "AddressBook-v22.abcddb"))
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                people = {r[0]: " ".join(x for x in (r[1], r[2]) if x) or (r[3] or "")
                          for r in con.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD")}
                for owner, number in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    if people.get(owner) and number:
                        found.setdefault(phone_key(number), people[owner])
                for owner, email in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    if people.get(owner) and email:
                        found.setdefault(phone_key(email), people[owner])
            finally:
                con.close()
        except sqlite3.Error:
            continue
    _names, _names_at = found, time.time()
    return found


def name_for(handle: str) -> str:
    return contact_names().get(phone_key(handle), handle) if handle else ""


# ── queries ──────────────────────────────────────────────────────────────────
_QUERY = """
SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date, m.service,
       h.id, c.display_name, m.associated_message_type, m.cache_has_attachments
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
"""


def _connect(db: Path | None = None):
    db = db or CHAT_DB
    if not db.exists():
        raise AccessError("Messages history wasn't found on this Mac.")
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        con.execute("SELECT 1 FROM message LIMIT 1")
        return con
    except sqlite3.Error as exc:
        if con is not None:
            con.close()
        raise AccessError(NEEDS_ACCESS) from exc


def _row(r) -> Text | None:
    rowid, text, body, from_me, date, service, handle, chat, assoc, attach = r
    if assoc:                                   # tapbacks / reactions, not messages
        return None
    words = text or decode_attributed_body(body)
    if not words and attach:
        words = "[attachment]"
    words = _clean(words)
    if not words:
        return None
    handle = handle or ""
    return Text(rowid=rowid, handle=handle, name=name_for(handle) if not from_me else "You",
                text=words, at=apple_time(date), from_me=bool(from_me),
                chat=chat or "", service=service or "")


def latest_rowid(db: Path | None = None) -> int:
    con = _connect(db)
    try:
        return int(con.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()[0])
    finally:
        con.close()


def since(rowid: int, limit: int = 50, fresh_seconds: float | None = None,
          db: Path | None = None) -> tuple[list[Text], int]:
    """(messages added after `rowid` oldest first, highest ROWID looked at).

    The second value moves the caller's bookmark past rows that are skipped
    (reactions, system rows), so a run of them can never stall it.
    `fresh_seconds` also drops messages older than that: when iCloud syncs old
    history down, it arrives with new, higher ROWIDs and must not be read out
    as if it had just been sent."""
    con = _connect(db)
    try:
        rows = con.execute(_QUERY + " WHERE m.ROWID > ? AND IFNULL(m.item_type, 0) = 0"
                           " ORDER BY m.ROWID ASC LIMIT ?", (int(rowid), int(limit))).fetchall()
        top = con.execute("SELECT IFNULL(MAX(ROWID), ?) FROM (SELECT ROWID FROM message"
                          " WHERE ROWID > ? ORDER BY ROWID ASC LIMIT ?)",
                          (int(rowid), int(rowid), int(limit))).fetchone()[0]
    finally:
        con.close()
    cutoff = time.time() - fresh_seconds if fresh_seconds else 0
    out, seen = [], set()
    for r in rows:
        t = _row(r)
        if t and t.rowid not in seen and t.at >= cutoff:
            seen.add(t.rowid)
            out.append(t)
    return out, int(top)


def recent(limit: int = 10, incoming_only: bool = True, db: Path | None = None) -> list[Text]:
    """The newest messages, newest first."""
    con = _connect(db)
    try:
        where = " WHERE m.is_from_me = 0" if incoming_only else ""
        rows = con.execute(_QUERY + where + " ORDER BY m.date DESC LIMIT ?",
                           (int(limit) * 3,)).fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for r in rows:
        t = _row(r)
        if t and t.rowid not in seen:
            seen.add(t.rowid)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def conversation(person: str, limit: int = 12, db: Path | None = None) -> tuple[str, list[Text]]:
    """(who, messages oldest→newest) for a contact name, number or email."""
    q = (person or "").strip().lower()
    if not q:
        return "", []
    names = contact_names()
    # Exact name, then whole word ("Al" must not match "Alex" or "Sally"),
    # then any part of a name.
    keys = {k for k, n in names.items() if n.lower() == q}
    if not keys:
        keys = {k for k, n in names.items() if q in n.lower().split()}
    if not keys:
        keys = {k for k, n in names.items() if q in n.lower()}
    by_name = bool(keys)
    if not keys:
        keys = {phone_key(person)}
    con = _connect(db)
    try:
        handles = [(rid, hid) for rid, hid in con.execute("SELECT ROWID, id FROM handle")
                   if phone_key(hid) in keys
                   or (not by_name and q in (hid or "").lower())]
        if not handles:
            return person, []
        ids = [h[0] for h in handles]
        marks = ",".join("?" * len(ids))
        rows = con.execute(
            _QUERY + f" WHERE m.ROWID IN (SELECT message_id FROM chat_message_join WHERE chat_id IN "
                     f"(SELECT chat_id FROM chat_handle_join WHERE handle_id IN ({marks})))"
                     " ORDER BY m.date DESC LIMIT ?", (*ids, int(limit) * 2)).fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for r in rows:
        t = _row(r)
        if t and t.rowid not in seen:
            seen.add(t.rowid)
            out.append(t)
    out = out[:limit][::-1]
    who = name_for(handles[0][1]) or person
    return who, out


def sent_to_recently(hours: float = 24.0, db: Path | None = None) -> set[str]:
    """phone_keys of people you messaged in the last `hours` (one-to-one or group)."""
    cutoff_ns = (time.time() - _APPLE_EPOCH - hours * 3600) * 1e9
    con = _connect(db)
    try:
        rows = con.execute(
            "SELECT DISTINCT h.id FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "JOIN chat_handle_join chj ON chj.chat_id = cmj.chat_id "
            "JOIN handle h ON h.ROWID = chj.handle_id "
            "WHERE m.is_from_me = 1 AND m.date > ?", (cutoff_ns,)).fetchall()
    finally:
        con.close()
    return {phone_key(r[0]) for r in rows if r[0]}
