"""Gmail for the campaign: headless, using the sign-in the user already gave Jarvis (spam account).

No window, no clicking, no login prompt: if the saved sign-in can't be used silently, the campaign
pauses with "connection missing" instead of trying to log in. Sends carry a private header
(X-Jarvis-Idem) so that after a crash or a timeout the Sent folder can be searched to find out whether
an email really went out.
"""
from __future__ import annotations

import base64
import re
import time
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional


class MailError(Exception):
    """kind: auth (sign-in problem), restricted (provider limit or block), rejected (bad recipient),
    unknown (timeout or network: outcome uncertain)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


def classify_error(exc: BaseException) -> MailError:
    status = None
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            status = int(getattr(resp, "status", 0) or 0)
        except (TypeError, ValueError):
            status = None
    text = str(exc).lower()
    if status in (401,) or "invalid_grant" in text or "token has been expired" in text or "refresh" in text and "token" in text:
        return MailError("auth", "Gmail sign-in is no longer valid")
    if status in (403, 429) or "ratelimit" in text or "quota" in text or "suspended" in text:
        return MailError("restricted", "Gmail refused or limited sending")
    if status in (400, 404, 422) and status is not None:
        return MailError("rejected", "Gmail rejected this message")
    return MailError("unknown", type(exc).__name__)


class GmailMailer:
    def __init__(self, account: str = "spam"):
        self.account = account
        self._service = None
        self._from: Optional[str] = None

    def _svc(self):
        if self._service is None:
            from plugins import gmail
            if self.account not in gmail._connected_accounts():
                raise MailError("auth", f"the {self.account} Gmail account is not connected")
            try:
                self._service = gmail._service(self.account, allow_login=False)
            except Exception as exc:
                raise classify_error(exc)
        return self._service

    def check(self) -> str:
        """Address mail goes out from. Raises MailError(auth) when the sign-in can't be used silently."""
        if self._from is None:
            try:
                from plugins import gmail
                prof = self._svc().users().getProfile(userId="me").execute()
                self._from = gmail._email_address(prof.get("emailAddress", ""))
            except MailError:
                raise
            except Exception as exc:
                raise classify_error(exc)
        return self._from

    def send(self, to: str, subject: str, body: str, idem_key: str) -> dict:
        frm = self.check()
        try:
            m = EmailMessage()
            m["To"], m["From"], m["Subject"], m["X-Jarvis-Idem"] = to, frm, subject, idem_key
            m.set_content(body)
            raw = base64.urlsafe_b64encode(m.as_bytes()).decode("ascii")
        except (ValueError, TypeError) as exc:
            raise MailError("rejected", f"message could not be built: {type(exc).__name__}")
        try:
            res = self._svc().users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as exc:
            raise classify_error(exc)
        if not res.get("id"):
            raise MailError("unknown", "Gmail did not return a message id")
        return {"message_id": res["id"], "thread_id": res.get("threadId", ""), "from": frm}

    def find_sent(self, row: dict) -> dict:
        """Did this outbox row go out? Looks in Sent for our private header. Authoritative 'not found' only
        when the search itself worked and covered the time window."""
        key, to = row["idem_key"], row["payload"].get("to", "")
        if not re.fullmatch(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", to or ""):
            return {"found": None}
        since = int(float(row.get("attempted_at") or row.get("created") or 0)) - 86400
        q = f'in:sent to:{to} after:{since}'
        try:
            svc = self._svc()
            found = svc.users().messages().list(userId="me", q=q, maxResults=20).execute().get("messages", []) or []
            for item in found:
                msg = svc.users().messages().get(userId="me", id=item["id"], format="metadata",
                                                 metadataHeaders=["X-Jarvis-Idem"]).execute()
                for h in msg.get("payload", {}).get("headers", []):
                    if h.get("name", "").lower() == "x-jarvis-idem" and h.get("value") == key:
                        return {"found": True, "message_id": msg["id"], "thread_id": msg.get("threadId", "")}
        except Exception:
            return {"found": None}                     # could not look: stays held
        return {"found": False, "authoritative": True}

    def thread_messages(self, thread_id: str) -> list[dict]:
        """Messages in a thread: [{id, ts, from, subject, text, headers}] (sent ones included)."""
        svc = self._svc()
        try:
            t = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        except Exception as exc:
            raise classify_error(exc)
        return [_conv(m) for m in t.get("messages", [])]

    def inbox_since(self, days: int = 3) -> list[dict]:
        """Recent incoming mail, so an opt-out or bounce that arrives as a NEW message (not in our thread) is seen."""
        svc = self._svc()
        try:
            found = svc.users().messages().list(userId="me", q=f"-in:sent newer_than:{int(days)}d", maxResults=100).execute().get("messages", []) or []
            return [_conv(svc.users().messages().get(userId="me", id=i["id"], format="full").execute()) for i in found]
        except Exception as exc:
            raise classify_error(exc)


def _conv(msg: dict) -> dict:
    h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
    return {"id": msg["id"], "ts": int(msg.get("internalDate") or 0) / 1000, "from": h.get("from", ""),
            "subject": h.get("subject", ""), "text": _text(msg.get("payload", {})) or msg.get("snippet", ""),
            "headers": h, "labels": msg.get("labelIds", [])}


def _text(payload: dict) -> str:
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
        except Exception:
            return ""
    for part in payload.get("parts", []) or []:
        t = _text(part)
        if t:
            return t
    return ""


def address_of(header: str) -> str:
    return parseaddr(header or "")[1].lower()
