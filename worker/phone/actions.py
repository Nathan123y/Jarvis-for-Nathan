"""What the phone assistant's tools actually do, and the write-up after a call.

    find_business  match the business the caller names to the campaign
    send_link      email their preview and the payment link from the campaign Gmail
    take_message   leave Nathan a note; "do not contact" suppresses the business
    after a call   one event for Jarvis to announce, and the call on the business

Settings live under Plugin Settings → Phone line (namespace "phone_line").
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from worker.phone import agent

CAMPAIGN = "bay-area-websites"
MAX_EMAILS_PER_CALL = 2
_EMAIL = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[A-Za-z]{2,}$")

OUTCOME_WORDS = {
    "wants_site": "wants the website",
    "has_questions": "has questions for you",
    "callback": "wants a call back",
    "not_interested": "isn't interested",
    "do_not_contact": "asked not to be contacted again",
    "other": "left a message",
}


def settings() -> dict:
    from memory.config_manager import get_plugin_config
    cfg = get_plugin_config("phone_line")
    return {
        "owner": str(cfg.get("owner_name") or "Nathan").strip(),
        "assistant": str(cfg.get("assistant_name") or "Ava").strip(),
        "price": str(cfg.get("price") or "249").strip().lstrip("$"),
        "hosting": str(cfg.get("hosting_monthly") or "").strip().lstrip("$"),
        "payment_link": str(cfg.get("payment_link") or "").strip(),
        "gmail": str(cfg.get("gmail_account") or "spam").strip().lower(),
    }


def email_text(owner: str, price: str, business: Optional[agent.Business], contact: str,
               payment_link: str) -> tuple[str, str]:
    name = business.name if business else "your business"
    hello = f"Hi {contact}," if contact else "Hello,"
    lines = [hello, "",
             f"Thanks for calling about a website for {name}."]
    if business and business.preview_url:
        lines += ["", "Here's the preview I put together for you:", business.preview_url]
    lines += ["",
              f"The full site is ${price}, paid once: a one-page website with your own branding and "
              "wording, built for phones first, with your services, hours, location and contact or "
              "booking links, plus one round of changes. It's usually ready within a week after you "
              "approve the wording.",
              "",
              "When you're ready, you can pay securely here:",
              payment_link,
              "",
              "Reply to this email with your logo, photos or any changes and I'll take it from there.",
              "",
              owner]
    return f"Your website for {name}", "\n".join(lines)


class PhoneActions:
    """Tool runner for one phone line. `store`, `events` and `send_email` are
    injected so tests never touch the real campaign, event log or Gmail."""

    def __init__(self, store, events, send_email: Callable[[str, str, str, str], str],
                 config: Optional[dict] = None, log: Callable[[str], None] = print):
        self.store, self.events, self.send_email = store, events, send_email
        self.config = config or settings()
        self.log = log

    def directory(self) -> agent.Directory:
        rows = [r for r in self.store.businesses(CAMPAIGN)
                if r.get("status") != "rejected" and r.get("stage") != "found"]
        return agent.Directory(rows)

    async def __call__(self, call, name: str, args: dict) -> str:
        if name == "find_business":
            return self.find_business(call, args)
        if name == "send_link":
            return self.send_link(call, args)
        if name == "take_message":
            return self.take_message(call, args)
        return f"Unknown tool {name}."

    def find_business(self, call, args: dict) -> str:
        found = call.directory.by_name(str(args.get("name") or ""), str(args.get("city") or ""))
        if not found:
            return ("That business isn't in Nathan's list. Still help them: explain the offer, and use "
                    "take_message so Nathan can build them a preview.")
        if len(found) > 1 and not args.get("city"):
            options = "; ".join(f"{b.name} ({b.city or b.address})" for b in found)
            return f"A few businesses match: {options}. Ask which one, or ask for the city."
        call.record.business = found[0]
        return "Found it. Confirm with the caller, then use these facts:\n" + found[0].facts()

    def send_link(self, call, args: dict) -> str:
        email = str(args.get("email") or "").strip().strip(".").lower()
        if not _EMAIL.fullmatch(email):
            return "That email address doesn't look complete. Ask for it again and read it back."
        sent = [a for a in call.record.actions if a[0] == "send_link" and a[2].startswith("Sent")]
        if len(sent) >= MAX_EMAILS_PER_CALL:
            return "Two emails already went out on this call. Tell them Nathan will follow up instead."
        if not self.config["payment_link"]:
            return ("The payment link isn't set up yet. Tell them Nathan will email it personally today, "
                    "and use take_message with their email.")
        business = call.record.business
        row = self.store.business(business.id) if business else None
        if self.store.is_suppressed(*_opt_out_keys(email, row)) or (row or {}).get("status") == "suppressed":
            return "This business asked not to be contacted. Don't send anything; say Nathan will follow up."
        subject, body = email_text(self.config["owner"], self.config["price"], business,
                                   str(args.get("contact_name") or "").strip(), self.config["payment_link"])
        try:
            self.send_email(self.config["gmail"], email, subject, body)
        except Exception as exc:
            self.log(f"[Phone] email to caller failed: {exc!r}")
            return "The email didn't go through. Apologise and say Nathan will send it himself today."
        return f"Sent to {email}. Tell them it should arrive in a minute and to check spam if not."

    def take_message(self, call, args: dict) -> str:
        outcome = str(args.get("outcome") or "other")
        call.record.outcome = outcome if outcome in OUTCOME_WORDS else "other"
        if call.record.outcome == "do_not_contact":
            # The same keys a "stop emailing me" reply blocks, so the campaign never mails them again.
            business = call.record.business
            row = self.store.business(business.id) if business else None
            for key in _opt_out_keys("", row):
                self.store.suppress(key, "opted_out", source="phone")
            if row:
                self.store.update_business(row["id"], status="suppressed", hold_reason="opted_out")
                self.store.audit(CAMPAIGN, "phone", "suppressed:opted_out", {"business_id": row["id"]})
            return "Noted: they won't be contacted again. Thank them and end the call."
        return "Noted for Nathan."

    def write_up(self, record) -> None:
        """After every call: one event for Jarvis to announce, and the call on the business."""
        b = record.business
        notes = [a[1] for a in record.actions if a[0] == "take_message"]
        emailed = [a[1].get("email", "") for a in record.actions
                   if a[0] == "send_link" and a[2].startswith("Sent")]
        outcome = record.outcome or ("wants_site" if emailed else "other")
        summary = "; ".join(str(n.get("summary", "")) for n in notes if n.get("summary"))
        title = (b.name if b else f"A caller from {record.caller or 'an unknown number'}")
        detail = {"caller": record.caller, "outcome": outcome, "summary": summary[:500],
                  "emailed": emailed, "seconds": round(record.ended - record.started),
                  "ended_by": record.ended_by,
                  "callback": next((n.get("callback_number") or n.get("callback_time") for n in notes
                                    if n.get("callback_number") or n.get("callback_time")), ""),
                  "transcript": [f"{who}: {text}" for who, text in record.transcript][-40:]}
        self.events.record("call_received", source="phone", source_id=f"call:{record.call_sid or record.started}",
                           ts=record.started, task_id=str(b.id) if b else "", campaign_id=CAMPAIGN,
                           status=outcome, title=title, detail=detail, private=True,
                           evidence={"call_sid": record.call_sid})
        if b:
            row = self.store.business(b.id) or {}
            calls = list((row.get("data") or {}).get("calls") or [])[-9:]
            calls.append({k: detail[k] for k in ("caller", "outcome", "summary", "emailed", "seconds")}
                         | {"at": record.started})
            self.store.update_business(b.id, data={"calls": calls})
            self.store.audit(CAMPAIGN, "phone", "call", {"business": b.id, "outcome": outcome,
                                                         "emailed": bool(emailed)})


def _opt_out_keys(email: str, row: Optional[dict]) -> list[str]:
    """The campaign's own opt-out keys: the addresses and the business's domain."""
    from worker.campaign.pipeline import suppression_keys
    keys = suppression_keys(email) if email else []
    if row:
        keys += suppression_keys(row.get("email") or "", row.get("website") or "")
    return list(dict.fromkeys(k for k in keys if k))


def gmail_sender(account: str, to: str, subject: str, body: str) -> str:
    """Send through Jarvis's Gmail plugin with the named account's saved sign-in."""
    from plugins import gmail
    service = gmail._service(account)
    sender = service.users().getProfile(userId="me").execute().get("emailAddress", "")
    return gmail._send(service, to, subject, body, sender)


def now() -> float:
    return time.time()
