"""Phone alerts from the background worker: find what's important, hand it to core/phone_alerts.

Runs on its own thread inside the worker (not as a queued job), so a long website
build never delays a reminder. Each scanner keeps a cursor in the events store and
starts from "now" the first time, so turning this on never texts a backlog.

  reminders  every pass   a Jarvis reminder whose time has come
  events     every pass   website customers (call request, interest, question, payment),
                          broken background jobs, a paper trade that lost more than loss_alert
  email      every 3 min  unread inbox mail Gmail marks important or that says it's urgent, any
                          mail from a VIP sender, and packages out for delivery / delivered /
                          with a problem (personal and school accounts; the outreach "spam"
                          account is skipped)
  calendar   every 2 min  an event starting within calendar_minutes; an Apple Reminder with a time
  canvas     every 15 min Canvas work due within canvas_hours
  fantasy    every 20 min an injury / status change on your teams, a starter who must be swapped out
  morning    once a day   at morning_time: weather, calendar, Canvas, fantasy, packages, overnight news
  trading    weekdays     at trading_time: the day's paper results
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Callable, Optional

from core.events import Store
from core.phone_alerts import Alerts, settings
from worker import sources

PASS_SECONDS = 30
EMAIL_EVERY = 180
CANVAS_EVERY = 900
REMINDER_LATE = 15 * 60       # a reminder found more than this late (the Mac slept) is not texted
EMAIL_QUERY = ("in:inbox is:unread newer_than:1d -category:promotions -category:social -category:forums "
               "(is:important OR subject:(urgent OR asap OR deadline OR emergency OR \"action required\"))")
EMAIL_ACCOUNTS = ("personal", "school")
CALENDAR_EVERY = 120
FANTASY_EVERY = 1200
PACKAGE_TEXT = {"out_for_delivery": "is out for delivery", "delivered": "was delivered",
                "problem": "has a delivery problem"}
APPLE_REMINDERS = "Scheduled Reminders"
NEEDS_YOU = {"call_request": "wants a call", "interested": "is interested", "question": "asked a question",
             "complaint": "complained"}

_CURSOR = "phone_alerts_cursor"


def _short(value, limit=90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def vip_query(vips: list[str]) -> str:
    terms = " OR ".join(v if (" " not in v and ("@" in v or "." in v)) else f'"{v}"' for v in vips)
    return f"in:inbox is:unread newer_than:1d from:({terms})"


def _hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(value).split(":"))
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else default
    except ValueError:
        return default


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

    # ── email: important, VIP senders, packages ──────────────────────────────
    def email(self, accounts: Optional[list] = None, service_for=None) -> int:
        from plugins import gmail
        from plugins import packages
        cfg = self.cfg()
        accounts = accounts if accounts is not None else [a for a in gmail._connected_accounts() if a in EMAIL_ACCOUNTS]
        service_for = service_for or gmail._service
        cur = self._cur()
        seen, pkgs = dict(cur.get("email") or {}), dict(cur.get("packages") or {})
        recent = [p for p in cur.get("pkg_recent") or [] if self.clock() - p[0] < 86400]
        vip_sig = "|".join(sorted(v.lower() for v in cfg["vip"]))
        vip_changed = cur.get("vip_sig", "") != vip_sig        # a newly added VIP's old mail is not texted
        queries = ([("vip", vip_query(cfg["vip"]))] if cfg["vip"] else []) + [("email", EMAIL_QUERY)]
        n = 0
        for account in accounts:
            try:
                service = service_for(account)
                api = service.users().messages()
                listed = [(kind, [m["id"] for m in api.list(userId="me", q=q, maxResults=10).execute()
                                  .get("messages", [])]) for kind, q in queries]
                pkg_ids = [m["id"] for m in api.list(userId="me", q=packages._QUERY, maxResults=20).execute()
                           .get("messages", [])]
            except Exception as exc:
                self.log(f"phone alerts: {account} Gmail check failed ({type(exc).__name__})")
                continue
            first, known = account not in seen, list(seen.get(account) or [])
            for kind, ids in listed:
                new = [i for i in ids if i not in known]
                if not first and not (kind == "vip" and vip_changed):
                    for found in gmail._fetch(service, new):
                        headers = gmail._headers(found[2])
                        name, address = parseaddr(headers.get("from", ""))
                        icon = "⭐" if kind == "vip" else "📧"
                        self.alerts.notify(kind, f"{icon} {account.title()} email from {_short(name or address, 40)}: "
                                                 f"{_short(headers.get('subject') or '(no subject)', 100)}")
                        n += 1
                known += new
            seen[account] = known[-300:]
            pk_first, pk_known = account not in pkgs, list(pkgs.get(account) or [])
            for mid in [i for i in pkg_ids if i not in pk_known]:
                pk_known.append(mid)
                if pk_first:
                    continue
                msg = api.get(userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"]).execute()
                if "CATEGORY_PROMOTIONS" in (msg.get("labelIds") or []):
                    continue
                h = gmail._headers(msg)
                status = packages.classify(h.get("subject", ""), msg.get("snippet", ""))
                if status in PACKAGE_TEXT:
                    name, address = parseaddr(h.get("from", ""))
                    line = f"{_short(name or address or 'A store', 30)}: your package {PACKAGE_TEXT[status]}"
                    self.alerts.notify("packages", f"📦 {line}.")
                    recent.append([self.clock(), status, line])
                    n += 1
            pkgs[account] = pk_known[-300:]
        self._put(email=seen, packages=pkgs, pkg_recent=recent[-20:], vip_sig=vip_sig)
        return n

    # ── calendar and Apple Reminders ─────────────────────────────────────────
    def calendar(self, events_fn=None) -> int:
        events_fn = events_fn or sources.calendar_events
        lead = timedelta(minutes=self.cfg()["calendar_minutes"])
        now = datetime.fromtimestamp(self.clock())
        warned = dict(self._cur().get("calendar") or {})
        first = "calendar" not in self._cur()
        n = 0
        for e in events_fn(now - timedelta(minutes=10), now + lead + timedelta(minutes=5)):
            if e["all_day"]:
                continue
            key = f"{e['at'].isoformat()}|{e['title']}"
            if key in warned:
                continue
            if e["calendar"] == APPLE_REMINDERS:
                if e["at"] <= now + timedelta(minutes=1):          # a reminder: at its time, not before
                    warned[key] = e["at"].timestamp()
                    if not first:
                        self.alerts.notify("reminders", f"⏰ Reminder: {e['title']}")
                        n += 1
            elif now < e["at"] <= now + lead:
                warned[key] = e["at"].timestamp()
                mins = max(1, round((e["at"] - now).total_seconds() / 60))
                self.alerts.notify("calendar", f"📅 {e['title']} at {sources.clock(e['at'])} (in {mins} min).")
                n += 1
        cutoff = self.clock() - 86400
        self._put(calendar={k: v for k, v in warned.items() if v > cutoff})
        return n

    # ── fantasy football ─────────────────────────────────────────────────────
    def fantasy(self, alerts_fn=None) -> int:
        if alerts_fn is None:
            from memory.config_manager import get_plugin_config, get_plugin_enabled
            from plugins import espn_fantasy
            if not get_plugin_enabled("espn_fantasy") or not espn_fantasy._league_ids(get_plugin_config("espn_fantasy")):
                return 0
            alerts_fn = espn_fantasy.roster_alerts
        found = alerts_fn()
        cur = self._cur()
        first = "fantasy" not in cur
        sent = dict(cur.get("fantasy") or {})
        n = 0
        lines = [("c:" + c, f"🏈 {c}.") for c in found.get("changes", [])] + \
                [("s:" + s, f"🏈 Swap out of your lineup: {s}.") for s in found.get("swap", [])]
        for key, text in lines:
            if key in sent:
                continue
            sent[key] = self.clock()
            if not first:
                self.alerts.notify("fantasy", text)
                n += 1
        cutoff = self.clock() - 7 * 86400
        self._put(fantasy={k: v for k, v in sent.items() if v > cutoff}, fantasy_swaps=found.get("swap", []))
        return n

    # ── the morning text ─────────────────────────────────────────────────────
    def morning(self, gather=None, force: bool = False) -> str:
        cfg = self.cfg()
        now = datetime.fromtimestamp(self.clock())
        h, m = _hhmm(cfg["morning_time"], (7, 0))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        cur = self._cur()
        today = now.date().isoformat()
        if not force:
            if cur.get("morning_day") == today or not (start <= now < start + timedelta(hours=4)):
                return ""
            self._put(morning_day=today)
        if not force and (not cfg["enabled"] or "morning" in cfg["off"]):
            return "off"
        parts = (gather or self._gather_morning)(cfg, now)
        text = sources.morning_text(today=now.date(), overnight=[] if force else self.alerts.take_held(), **parts)
        return self.alerts.scheduled("morning", text, force=force)

    def _gather_morning(self, cfg: dict, now: datetime) -> dict:
        def safe(fn, default):
            try:
                return fn()
            except Exception as exc:
                self.log(f"phone alerts: morning text skipped a part: {exc!r}")
                return default

        def canvas():
            from plugins.canvas_school import fetch_assignments
            items, notice = fetch_assignments("upcoming")
            return [] if notice else items

        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cur = self._cur()
        return {
            "weather_line": safe(lambda: sources.weather(cfg["city"]), None),
            "events": safe(lambda: sources.calendar_events(day, day + timedelta(days=1)), None),
            "canvas": safe(canvas, []),
            "fantasy_swaps": list(cur.get("fantasy_swaps") or []),
            "packages": [p[2] for p in cur.get("pkg_recent") or [] if p[1] == "out_for_delivery"],
        }

    # ── the trading day ──────────────────────────────────────────────────────
    def trading_daily(self, summary_fn=None, force: bool = False) -> str:
        cfg = self.cfg()
        now = datetime.fromtimestamp(self.clock())
        h, m = _hhmm(cfg["trading_time"], (13, 30))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        today = now.date().isoformat()
        if not force:
            if now.weekday() >= 5 or self._cur().get("trading_day") == today or not (start <= now < start + timedelta(hours=5)):
                return ""
            self._put(trading_day=today)
            if not cfg["enabled"] or "trading_daily" in cfg["off"]:
                return "off"
        text = (summary_fn or sources.trading_summary)()
        return self.alerts.scheduled("trading_daily", f"📈 Paper trading today: {text}", force=force) if text else ""

    def on_request(self) -> str:
        """'Text me my morning summary now' / 'text me the trading summary now', from the plugin."""
        from core.phone_alerts import NOW_FLAG
        what = self.store.get(NOW_FLAG)
        if not what:
            return ""
        self.store.set(NOW_FLAG, "")
        return self.morning(force=True) if what == "morning" else self.trading_daily(force=True)

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
        self._last = {"email": 0.0, "canvas": 0.0, "calendar": 0.0, "fantasy": 0.0}

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
        for name, every in (("calendar", CALENDAR_EVERY), ("canvas", CANVAS_EVERY), ("fantasy", FANTASY_EVERY)):
            if now - self._last[name] >= every:
                self._last[name] = now
                self._step(name, getattr(self.scanner, name))
        self._step("on request", self.scanner.on_request)
        self._step("morning", self.scanner.morning)          # before flush: it carries the overnight news
        self._step("trading", self.scanner.trading_daily)
        self._step("flush", self.alerts.flush)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(PASS_SECONDS)
