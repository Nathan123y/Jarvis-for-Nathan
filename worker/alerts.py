"""Phone alerts from the background worker: find what's important, hand it to core/phone_alerts.

Runs on its own thread inside the worker (not as a queued job), so a long website
build never delays a reminder. Each scanner keeps a cursor in the events store and
starts from "now" the first time, so turning this on never texts a backlog.

  reminders  every pass   a Jarvis reminder whose time has come
  events     every pass   website customers (call request, interest, question, payment),
                          broken background jobs, a paper trade that lost more than loss_alert
  email      every 3 min  unread inbox mail Gmail marks important or that says it's urgent
                          (personal and school accounts; the outreach "spam" account is skipped)
  canvas     every 15 min Canvas work due within canvas_hours
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Callable, Optional

from core.events import Store
from core.phone_alerts import Alerts, settings

PASS_SECONDS = 30
EMAIL_EVERY = 180
CANVAS_EVERY = 900
REMINDER_LATE = 15 * 60       # a reminder found more than this late (the Mac slept) is not texted
EMAIL_QUERY = ("in:inbox is:unread newer_than:1d -category:promotions -category:social -category:forums "
               "(is:important OR subject:(urgent OR asap OR deadline OR emergency OR \"action required\"))")
EMAIL_ACCOUNTS = ("personal", "school")
NEEDS_YOU = {"call_request": "wants a call", "interested": "is interested", "question": "asked a question",
             "complaint": "complained"}

_CURSOR = "phone_alerts_cursor"


def _short(value, limit=90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Scanner:
    def __init__(self, store: Store, alerts: Alerts, *, clock: Callable[[], float] = time.time,
                 cfg: Callable[[], dict] = settings, log: Callable[[str], None] = lambda m: None):
        self.store, self.alerts, self.clock, self.cfg, self.log = store, alerts, clock, cfg, log

    # cursors live in one meta row
    def _cur(self) -> dict:
        return dict(self.store.get(_CURSOR) or {})

    def _put(self, **kv) -> None:
        cur = self._cur()
        cur.update(kv)
        self.store.set(_CURSOR, cur)

    # ── reminders ────────────────────────────────────────────────────────────
    def reminders(self, pending: Optional[Callable[[], list]] = None) -> int:
        """Remember upcoming reminders; text each one when its time comes. (Its own launchd job
        deletes its script as it fires, so it has to be seen beforehand.)"""
        if pending is None:
            from actions.reminder import pending_reminders as pending
        now = self.clock()
        seen = dict(self._cur().get("reminders") or {})
        for when, message in pending(now=datetime.fromtimestamp(now)):
            seen.setdefault(f"{when.timestamp():.0f}|{message}", {"at": when.timestamp(), "msg": message})
        sent = 0
        for key, r in list(seen.items()):
            if r["at"] > now:
                continue
            del seen[key]
            if now - r["at"] <= REMINDER_LATE:
                self.alerts.notify("reminders", f"⏰ Reminder: {_short(r['msg'], 200)}")
                sent += 1
        self._put(reminders=seen)
        return sent

    # ── website business, broken jobs, trading ───────────────────────────────
    def events(self) -> int:
        cur = self._cur()
        if "event_id" not in cur:
            self._put(event_id=self.store.max_id())
            return 0
        evs = self.store.events(after_id=int(cur["event_id"]), limit=500)
        cfg, n = self.cfg(), 0
        for e in evs:
            msg = self._event_text(e, cfg)
            if msg:
                kind, text = msg
                self.alerts.notify(kind, text)
                n += 1
        if evs:
            self._put(event_id=evs[-1]["id"])
        return n

    @staticmethod
    def _event_text(e: dict, cfg: dict) -> Optional[tuple[str, str]]:
        kind, title, d = e["kind"], _short(e.get("title"), 60), e.get("detail") or {}
        if kind == "reply_received" and e.get("status") in NEEDS_YOU:
            return "business", f"💼 {title} {NEEDS_YOU[e['status']]} about the website offer."
        if kind == "call_received":
            return "business", f"📞 Business line call: {title}."
        if kind == "sale_paid":
            return "business", f"💰 Payment received: {title}."
        if kind == "decision_needed":
            return "business", f"💼 Needs your decision: {title}."
        if kind == "campaign_stopped":
            return "business", f"⚠️ Website campaign stopped: {title}."
        if kind in ("job_failed", "connection_missing"):
            return "system", f"⚠️ {title}."
        if kind == "trade_result":
            try:
                pnl = float(d.get("pnl"))
            except (TypeError, ValueError):
                return None
            if pnl <= -abs(cfg["loss_alert"]):
                return "trading", (f"📉 Paper trade lost ${-pnl:,.0f}: {_short(d.get('bot'), 30)} "
                                   f"{_short(d.get('symbol'), 40)} (simulated).")
        return None

    # ── email ────────────────────────────────────────────────────────────────
    def email(self, accounts: Optional[list] = None, service_for=None) -> int:
        from plugins import gmail
        accounts = accounts if accounts is not None else [a for a in gmail._connected_accounts() if a in EMAIL_ACCOUNTS]
        service_for = service_for or gmail._service
        cur = self._cur()
        seen = dict(cur.get("email") or {})
        n = 0
        for account in accounts:
            try:
                service = service_for(account)
                ids = [m["id"] for m in service.users().messages().list(
                    userId="me", q=EMAIL_QUERY, maxResults=10).execute().get("messages", [])]
            except Exception as exc:
                self.log(f"phone alerts: {account} Gmail check failed ({type(exc).__name__})")
                continue
            known = set(seen.get(account) or [])
            first = account not in seen
            for found in gmail._fetch(service, [i for i in ids if i not in known]) if not first else []:
                headers = gmail._headers(found[2])
                name, address = parseaddr(headers.get("from", ""))
                sender = _short(name or address, 40)
                self.alerts.notify("email", f"📧 {account.title()} email from {sender}: "
                                            f"{_short(headers.get('subject') or '(no subject)', 100)}")
                n += 1
            seen[account] = (list(known | set(ids)))[-200:]
        self._put(email=seen)
        return n

    # ── Canvas ───────────────────────────────────────────────────────────────
    def canvas(self, fetch=None) -> int:
        if fetch is None:
            from plugins.canvas_school import fetch_assignments as fetch
        items, notice = fetch("upcoming")
        if notice:
            return 0
        now = datetime.fromtimestamp(self.clock())
        horizon = now + timedelta(hours=self.cfg()["canvas_hours"])
        warned = dict(self._cur().get("canvas") or {})
        n = 0
        for it in items:
            if it.get("overdue"):
                continue
            # The Canvas calendar feed gives only a date for work due at 11:59 PM.
            due_at = it.get("due_at") or (f"{it['due']}T23:59" if it.get("due") else "")
            try:
                when = datetime.fromisoformat(due_at)
            except ValueError:
                continue
            key = f"{it.get('course')}|{it.get('name')}|{due_at}"
            if now < when <= horizon and key not in warned:
                self.alerts.notify("canvas", f"📚 Due at {when.strftime('%-I:%M %p')}: "
                                             f"{_short(it.get('name'), 80)} ({_short(it.get('course'), 30)}).")
                warned[key] = when.timestamp()
                n += 1
        cutoff = self.clock() - 86400
        self._put(canvas={k: v for k, v in warned.items() if v > cutoff})
        return n


class AlertService:
    """The worker's alert thread."""

    def __init__(self, store: Store, log: Callable[[str], None] = print):
        self.log = log
        self.alerts = Alerts(store, log=log)
        self.scanner = Scanner(store, self.alerts, log=log)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last = {"email": 0.0, "canvas": 0.0}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="phone-alerts")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.log(f"phone alerts: {name} check failed: {exc!r}")

    def run_once(self) -> None:
        self._step("test", self.alerts.test_if_asked)
        if not settings()["enabled"]:
            return
        now = time.time()
        self._step("reminders", self.scanner.reminders)
        self._step("events", self.scanner.events)
        if now - self._last["email"] >= EMAIL_EVERY:
            self._last["email"] = now
            self._step("email", self.scanner.email)
        if now - self._last["canvas"] >= CANVAS_EVERY:
            self._last["canvas"] = now
            self._step("canvas", self.scanner.canvas)
        self._step("flush", self.alerts.flush)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(PASS_SECONDS)
