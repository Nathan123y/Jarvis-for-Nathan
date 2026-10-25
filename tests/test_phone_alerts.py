"""Phone alerts: when a text goes out, what the worker texts about, and lid-closed keep-awake."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import phone_alerts as pa
from core.events import Store
from worker import alerts as wa
from worker.runtime import KeepAwake, lid_sleep_disabled

NOON = datetime(2026, 10, 23, 12, 0).timestamp()


def _cfg(**over):
    cfg = dict(pa.DEFAULTS)
    cfg.update(over)
    return lambda: cfg


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "events.db")
        self.sent = []
        self.now = NOON
        self.away = True

    def alerts(self, **cfg):
        return pa.Alerts(self.store, send=self.sent.append, away=lambda m: self.away,
                         clock=lambda: self.now, cfg=_cfg(**cfg))


class AlertRulesTests(_Base):
    def test_sends_only_while_away(self):
        a = self.alerts()
        self.away = False
        self.assertEqual(a.notify("email", "hi"), "present")
        self.away = True
        self.assertEqual(a.notify("email", "hi"), "sent")
        self.assertEqual(self.sent, ["hi"])

    def test_muted_kind_and_switched_off(self):
        self.assertEqual(self.alerts(off=["canvas"]).notify("canvas", "x"), "off")
        self.assertEqual(self.alerts(enabled=False).notify("email", "x"), "off")
        self.assertEqual(self.sent, [])

    def test_quiet_hours_hold_everything_but_reminders_then_send_one_text(self):
        self.now = datetime(2026, 10, 23, 2, 0).timestamp()
        a = self.alerts()
        self.assertEqual(a.notify("email", "mail one"), "held")
        self.assertEqual(a.notify("business", "customer"), "held")
        self.assertEqual(a.notify("reminders", "take meds"), "sent")
        self.assertEqual(a.flush(), "quiet")
        self.now = datetime(2026, 10, 23, 7, 5).timestamp()
        self.assertEqual(a.flush(), "sent")
        self.assertEqual(self.sent[0], "take meds")
        self.assertIn("2 things while you were away", self.sent[1])
        self.assertIn("• customer", self.sent[1])

    def test_held_items_are_dropped_once_back_at_the_mac(self):
        a = self.alerts(max_per_hour=1)
        a.notify("email", "one")
        self.assertEqual(a.notify("email", "two"), "held")
        self.now += 3601
        self.away = False
        self.assertEqual(a.flush(), "present")
        self.assertEqual(self.sent, ["one"])

    def test_quiet_hours_across_midnight(self):
        self.assertTrue(pa.in_quiet_hours(datetime(2026, 1, 1, 23, 30), 23, 7))
        self.assertTrue(pa.in_quiet_hours(datetime(2026, 1, 1, 6, 59), 23, 7))
        self.assertFalse(pa.in_quiet_hours(datetime(2026, 1, 1, 12), 23, 7))
        self.assertFalse(pa.in_quiet_hours(datetime(2026, 1, 1, 3), 7, 7))

    def test_away_means_locked_or_idle(self):
        self.assertTrue(pa.is_away(5, screen="locked", idle=0))
        self.assertTrue(pa.is_away(5, screen="unlocked", idle=301))
        self.assertFalse(pa.is_away(5, screen="unlocked", idle=30))

    def test_test_text_goes_out_even_at_the_mac(self):
        self.away = False
        self.store.set(pa.TEST_FLAG, 1)
        a = self.alerts()
        self.assertEqual(a.test_if_asked(), "sent")
        self.assertEqual(a.test_if_asked(), "")
        self.assertEqual(len(self.sent), 1)


class ScannerTests(_Base):
    def scanner(self, **cfg):
        return wa.Scanner(self.store, self.alerts(**cfg), clock=lambda: self.now, cfg=_cfg(**cfg))

    def test_reminder_seen_ahead_is_texted_when_due(self):
        s = self.scanner()
        when = datetime.fromtimestamp(NOON) + timedelta(minutes=2)
        s.reminders(pending=lambda now: [(when, "Call the dentist")])
        self.assertEqual(self.sent, [])
        self.now = NOON + 150
        s.reminders(pending=lambda now: [])            # its launchd job already removed it
        self.assertEqual(self.sent, ["⏰ Reminder: Call the dentist"])
        s.reminders(pending=lambda now: [])
        self.assertEqual(len(self.sent), 1)

    def test_reminder_found_long_after_its_time_is_not_texted(self):
        s = self.scanner()
        when = datetime.fromtimestamp(NOON) + timedelta(minutes=1)
        s.reminders(pending=lambda now: [(when, "old")])
        self.now = NOON + 3600
        s.reminders(pending=lambda now: [])
        self.assertEqual(self.sent, [])

    def test_events_start_from_now_then_text_what_needs_you(self):
        self.store.record("sale_paid", source="x", source_id="old", title="Old sale")
        s = self.scanner(loss_alert=500)
        self.assertEqual(s.events(), 0)
        self.store.record("reply_received", source="gmail", source_id="r1", status="call_request", title="Joe's Tacos")
        self.store.record("reply_received", source="gmail", source_id="r2", status="declined", title="Nope Inc")
        self.store.record("trade_result", source="trading", source_id="t1", title="t",
                          detail={"pnl": -650.0, "bot": "momo", "symbol": "SPY 600C"})
        self.store.record("trade_result", source="trading", source_id="t2", title="t",
                          detail={"pnl": -120.0, "bot": "momo", "symbol": "SPY 600C"})
        self.assertEqual(s.events(), 2)
        self.assertIn("Joe's Tacos wants a call", self.sent[0])
        self.assertIn("lost $650", self.sent[1])
        self.assertEqual(s.events(), 0)

    def _gmail(self, results: dict, meta: dict | None = None):
        """A fake Gmail whose search results depend on the query: keys 'vip', 'email', 'packages'."""
        service = MagicMock()
        api = service.users.return_value.messages.return_value

        def listing(userId, q, maxResults):
            kind = "vip" if q.startswith("in:inbox is:unread newer_than:1d from:") else \
                "packages" if "out for delivery" in q else "email"
            return MagicMock(execute=MagicMock(return_value={"messages": [{"id": i} for i in results.get(kind, [])]}))
        api.list.side_effect = listing
        api.get.side_effect = lambda userId, id, **kw: MagicMock(execute=MagicMock(return_value=(meta or {})[id]))
        return service

    @staticmethod
    def _meta(sender, subject, snippet=""):
        return {"payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]},
                "snippet": snippet, "labelIds": ["INBOX"]}

    def test_email_first_pass_is_a_baseline_then_new_mail_is_texted(self):
        results = {"email": ["m1"]}
        s = self.scanner()
        meta = {"m2": self._meta("Ms. Lee <lee@school.edu>", "Midterm moved")}
        service = self._gmail(results, meta)
        with patch("plugins.gmail._fetch", side_effect=lambda svc, ids: [(1, i, meta[i]) for i in ids]) as fetch:
            self.assertEqual(s.email(accounts=["school"], service_for=lambda a: service), 0)
            fetch.assert_not_called()
            results["email"] = ["m2", "m1"]
            self.assertEqual(s.email(accounts=["school"], service_for=lambda a: service), 1)
        self.assertEqual(self.sent, ["📧 School email from Ms. Lee: Midterm moved"])

    def test_vip_mail_is_texted_once_and_a_new_vip_does_not_text_old_mail(self):
        results = {"email": [], "vip": ["v1"]}
        meta = {"v1": self._meta("Coach Dan <dan@club.org>", "Practice moved"),
                "v2": self._meta("Coach Dan <dan@club.org>", "Game at 3")}
        service = self._gmail(results, meta)
        fetch = lambda svc, ids: [(1, i, meta[i]) for i in ids]
        s = self.scanner()
        with patch("plugins.gmail._fetch", side_effect=fetch):
            s.email(accounts=["personal"], service_for=lambda a: service)        # baseline, no VIPs yet
            s.cfg = _cfg(vip=["dan@club.org"])
            s.alerts.cfg = s.cfg
            s.email(accounts=["personal"], service_for=lambda a: service)        # VIP just added: v1 is old
            self.assertEqual(self.sent, [])
            results["vip"] = ["v2", "v1"]
            results["email"] = ["v2"]                                            # also "important": one text only
            s.email(accounts=["personal"], service_for=lambda a: service)
        self.assertEqual(self.sent, ["⭐ Personal email from Coach Dan: Game at 3"])

    def test_package_out_for_delivery_is_texted_and_kept_for_the_morning(self):
        results = {"packages": ["p1"]}
        meta = {"p2": self._meta("Amazon <ship@amazon.com>", "Your package is out for delivery"),
                "p3": self._meta("Amazon <ship@amazon.com>", "Your order has shipped")}
        service = self._gmail(results, meta)
        s = self.scanner()
        s.email(accounts=["personal"], service_for=lambda a: service)
        results["packages"] = ["p3", "p2", "p1"]
        s.email(accounts=["personal"], service_for=lambda a: service)
        self.assertEqual(self.sent, ["📦 Amazon: your package is out for delivery."])
        self.assertEqual(s._gather_morning(pa.DEFAULTS, datetime.fromtimestamp(NOON))["packages"],
                         ["Amazon: your package is out for delivery"])

    def test_vip_query_quotes_names_and_keeps_addresses(self):
        self.assertEqual(wa.vip_query(["Ms. Lee", "lee@school.edu", "sjsu.edu"]),
                         'in:inbox is:unread newer_than:1d from:("Ms. Lee" OR lee@school.edu OR sjsu.edu)')

    def test_calendar_event_texts_ahead_and_apple_reminder_texts_on_time(self):
        s = self.scanner(calendar_minutes=15)
        at = lambda m: datetime.fromtimestamp(NOON) + timedelta(minutes=m)
        events = [{"at": at(12), "title": "EE-98 lecture", "all_day": False, "calendar": "School"},
                  {"at": at(40), "title": "Later thing", "all_day": False, "calendar": "School"},
                  {"at": at(0), "title": "Holiday", "all_day": True, "calendar": "Home"},
                  {"at": at(5), "title": "Pay credit card", "all_day": False, "calendar": "Scheduled Reminders"}]
        s.calendar(events_fn=lambda a, b: events)
        self.assertEqual(self.sent, ["📅 EE-98 lecture at 12:12 PM (in 12 min)."])
        self.now = NOON + 5 * 60
        s.calendar(events_fn=lambda a, b: events)
        self.assertEqual(self.sent[1:], ["⏰ Reminder: Pay credit card"])
        s.calendar(events_fn=lambda a, b: events)
        self.assertEqual(len(self.sent), 2)

    def test_fantasy_first_check_is_a_baseline_then_new_problems_are_texted(self):
        s = self.scanner()
        found = {"changes": ["Kelce (League A) is now Questionable (was healthy)"], "swap": [], "watch": []}
        s.fantasy(alerts_fn=lambda: found)
        self.assertEqual(self.sent, [])
        found = {"changes": found["changes"] + ["Hall (League A) is now Out (was Questionable), and he's in your starting lineup"],
                 "swap": ["Hall (League A, Out)"], "watch": []}
        self.assertEqual(s.fantasy(alerts_fn=lambda: found), 2)
        self.assertIn("Hall (League A) is now Out", self.sent[0])
        self.assertEqual(self.sent[1], "🏈 Swap out of your lineup: Hall (League A, Out).")
        self.assertEqual(s.fantasy(alerts_fn=lambda: found), 0)

    def test_morning_text_once_a_day_at_the_set_time_with_the_overnight_news(self):
        self.now = datetime(2026, 10, 23, 2, 0).timestamp()
        s = self.scanner()
        s.alerts.notify("email", "📧 overnight mail")                      # held: quiet hours
        gather = lambda cfg, now: {"weather_line": "San Jose: 70°/54°", "events": [], "canvas": [],
                                   "fantasy_swaps": [], "packages": []}
        self.assertEqual(s.morning(gather=gather), "")
        self.away = False                                                 # sent even at the Mac
        self.now = datetime(2026, 10, 23, 7, 0, 30).timestamp()
        self.assertEqual(s.morning(gather=gather), "sent")
        self.assertIn("Good morning! Fri Oct 23", self.sent[0])
        self.assertIn("🌙 Overnight: 📧 overnight mail", self.sent[0])
        self.assertEqual(s.alerts.flush(), "empty")
        self.assertEqual(s.morning(gather=gather), "")

    def test_trading_summary_on_weekdays_after_the_close(self):
        s = self.scanner()
        summary = lambda: "Options: up $120 today."
        self.now = datetime(2026, 10, 23, 13, 0).timestamp()
        self.assertEqual(s.trading_daily(summary_fn=summary), "")
        self.now = datetime(2026, 10, 23, 13, 31).timestamp()
        with patch("worker.sources.meme_status", return_value={"equity": 1040, "first_equity": 1000, "positions": []}), \
                patch("worker.sources.meme_trades", return_value=[]):
            self.assertEqual(s.trading_daily(summary_fn=summary), "sent")
        self.assertEqual(self.sent, ["📈 Paper trading today: Options: up $120 today.\n"
                                     "🪙 Memecoin account $1,040.00 (+40.00 since it started) (simulated)."])
        self.assertEqual(s.trading_daily(summary_fn=summary), "")
        self.now = datetime(2026, 10, 24, 13, 31).timestamp()             # Saturday
        self.assertEqual(s.trading_daily(summary_fn=summary), "")

    def test_send_later_waits_for_its_time(self):
        s = self.scanner()
        self.store.set(pa.NOW_FLAG, {"kind": "fantasy", "at": NOON + 180})
        with patch("worker.sources.fantasy_summary", return_value="League A: you 88.0 vs Bob 70.0"):
            self.assertEqual(s.on_request(), "")
            self.now = NOON + 181
            self.assertEqual(s.on_request(), "sent")
        self.assertEqual(self.sent, ["🏈 Fantasy this week:\nLeague A: you 88.0 vs Bob 70.0"])

    def test_send_now_from_the_plugin_ignores_the_schedule(self):
        s = self.scanner(off=["morning"])
        self.store.set(pa.NOW_FLAG, "morning")
        with patch.object(s, "_gather_morning", return_value={"weather_line": None, "events": [], "canvas": [],
                                                              "fantasy_swaps": [], "packages": []}):
            self.assertEqual(s.on_request(), "sent")
        self.assertIn("Good morning!", self.sent[0])
        self.assertEqual(s.on_request(), "")

    def test_canvas_warns_once_inside_the_window(self):
        s = self.scanner(canvas_hours=3)
        soon = (datetime.fromtimestamp(NOON) + timedelta(hours=2)).isoformat(timespec="minutes")
        later = (datetime.fromtimestamp(NOON) + timedelta(hours=8)).isoformat(timespec="minutes")
        items = [{"name": "Lab 4", "course": "EE 97", "due": soon[:10], "due_at": soon, "overdue": False},
                 {"name": "Essay", "course": "English", "due": later[:10], "due_at": later, "overdue": False},
                 {"name": "Tonight", "course": "Math", "due": soon[:10], "due_at": "", "overdue": False}]
        self.assertEqual(s.canvas(fetch=lambda tf: (items, None)), 1)
        self.assertEqual(s.canvas(fetch=lambda tf: (items, None)), 0)
        self.assertIn("Lab 4 (EE 97)", self.sent[0])
        self.assertIn("2:00 PM", self.sent[0])
        self.now = datetime(2026, 10, 23, 21, 30).timestamp()   # date-only work counts as due 11:59 PM
        self.assertEqual(s.canvas(fetch=lambda tf: (items, None)), 1)
        self.assertIn("Due at 11:59 PM: Tonight (Math)", self.sent[1])

    def test_due_tonight_once_at_eight_and_no_second_canvas_warning(self):
        s = self.scanner()
        items = [{"name": "Lab 4", "course": "EE 97", "due": "2026-10-23", "due_at": "", "overdue": False},
                 {"name": "Quiz", "course": "Math", "due": "2026-10-23", "due_at": "2026-10-23T19:00", "overdue": False},
                 {"name": "Essay", "course": "English", "due": "2026-10-24", "due_at": "", "overdue": False}]
        fetch = lambda tf: (items, None)
        self.assertEqual(s.canvas_tonight(fetch=fetch), "")                  # noon: not yet
        self.now = datetime(2026, 10, 23, 20, 1).timestamp()
        self.away = False                                                     # comes at the Mac too
        with patch("memory.config_manager.get_plugin_config", return_value={}):
            self.assertEqual(s.canvas_tonight(fetch=fetch), "sent")
            self.assertEqual(s.canvas_tonight(fetch=fetch), "")
        self.assertEqual(self.sent, ["📚 Still due tonight (I can't see what you've turned in):\n"
                                     "• 11:59 PM: Lab 4 (EE 97)"])
        self.away = True
        self.now = datetime(2026, 10, 23, 21, 30).timestamp()
        self.assertEqual(s.canvas(fetch=fetch), 0)

    def test_grades_baseline_then_new_grades_texted_once(self):
        s = self.scanner()
        grade = {"id": "9", "course": "EE 97", "name": "Lab 3", "score": 18, "possible": 20, "grade": "18"}
        self.assertEqual(s.grades(fetch=lambda since: ([grade], None)), 0)
        self.assertEqual(s.grades(fetch=lambda since: ([grade], None)), 1)
        self.assertEqual(s.grades(fetch=lambda since: ([grade], None)), 0)
        self.assertEqual(self.sent, ["🎓 Grade posted in EE 97: Lab 3, 18/20 (90%)."])
        self.assertEqual(s._cur()["grades_week"][0][1], "Lab 3 18/20 (90%)")

    def test_bots_review_texts_each_new_review_once(self):
        s = self.scanner()
        r1 = {"generated": "2026-10-22T16:36", "speech": "No changes."}
        r2 = {"generated": "2026-10-23T16:36", "speech": "qqq-orb-day tightened its stop."}
        self.assertEqual(s.bots_review(read=lambda: r1), "")                 # baseline
        self.assertEqual(s.bots_review(read=lambda: r1), "")
        self.assertEqual(s.bots_review(read=lambda: r2), "sent")
        self.assertEqual(s.bots_review(read=lambda: r2), "")
        self.assertEqual(self.sent, ["🧠 The bots' nightly review: qqq-orb-day tightened its stop."])

    def test_memecoin_trades_and_big_moves(self):
        s = self.scanner(meme_move=20)
        beat = datetime.fromtimestamp(NOON).astimezone().isoformat()
        trades = [{"kind": "buy", "symbol": "DOGE/USD", "usd": 100, "price": 0.21, "stop": 0.19}]
        status = {"heartbeat": beat, "positions": [{"symbol": "PEPE/USD", "opened": "x", "change_pct": 12}]}
        self.assertEqual(s.memecoin(trades_fn=lambda: trades, status_fn=lambda: status, hype_fn=lambda: None), 0)   # baseline
        trades = trades + [{"kind": "buy", "symbol": "PEPE/USD", "usd": 150, "price": 0.0000123, "stop": 0.00001},
                           {"kind": "sell", "symbol": "DOGE/USD", "why": "trailing stop", "pnl": 12.4}]
        status["positions"][0]["change_pct"] = 24
        self.assertEqual(s.memecoin(trades_fn=lambda: trades, status_fn=lambda: status, hype_fn=lambda: None), 3)
        self.assertEqual(s.memecoin(trades_fn=lambda: trades, status_fn=lambda: status, hype_fn=lambda: None), 0)
        self.assertEqual(self.sent, [
            "🪙 Memecoin bot bought PEPE/USD: $150 at $0.0000123, stop $0.0000100 (simulated).",
            "🪙 Memecoin bot sold DOGE/USD (trailing stop): +12.40 dollars (simulated).",
            "🪙 PEPE/USD is +24% since the memecoin bot bought it (simulated)."])

    def test_memecoin_profit_steps_are_texted(self):
        s = self.scanner(meme_profit_step=25)
        beat = datetime.fromtimestamp(NOON).astimezone().isoformat()
        st = lambda eq: (lambda: {"heartbeat": beat, "equity": eq, "first_equity": 1000, "positions": []})
        none = lambda: []
        for eq in (1010, 1030, 1040, 1052, 1020, 1027, 960):
            s.memecoin(trades_fn=none, status_fn=st(eq), hype_fn=lambda: None)
        self.assertEqual(self.sent, [
            "💰 The memecoin account is up $30.00 since it started (now $1,030.00, simulated).",
            "💰 The memecoin account is up $52.00 since it started (now $1,052.00, simulated).",
            "💰 The memecoin account is up $27.00 since it started (now $1,027.00, simulated).",
            "📉 The memecoin account is down $40.00 since it started (now $960.00, simulated)."])

    def test_a_coin_turning_hot_is_texted_once_per_hot_spell(self):
        s = self.scanner()
        ts = datetime.fromtimestamp(NOON).astimezone().isoformat()
        reading = lambda levels: (lambda: {"ts": ts, "coins": {k: {"level": v, "trending": 3 if k == "PEPE/USD" else None,
                                                                   "score": 2.4} for k, v in levels.items()}})
        none = lambda: []
        self.assertEqual(s.memecoin(trades_fn=none, status_fn=lambda: None, hype_fn=reading({"PEPE/USD": "hot"})), 0)  # baseline
        s.memecoin(trades_fn=none, status_fn=lambda: None, hype_fn=reading({"PEPE/USD": "normal", "DOGE/USD": "normal"}))
        self.assertEqual(s.memecoin(trades_fn=none, status_fn=lambda: None,
                                    hype_fn=reading({"PEPE/USD": "hot", "DOGE/USD": "hot"})), 2)
        self.assertEqual(s.memecoin(trades_fn=none, status_fn=lambda: None,
                                    hype_fn=reading({"PEPE/USD": "hot", "DOGE/USD": "hot"})), 0)
        self.assertIn("🔥 DOGE is getting hyped: 2.4x its usual buzz", self.sent[0])
        self.assertIn("🔥 PEPE is getting hyped: #3 trending on CoinGecko", self.sent[1])

    def test_gumroad_sales_baseline_then_new_sale(self):
        s = self.scanner()
        old = {"id": "a", "product": "Soccer Coach Organizer", "price": "$12", "refunded": False}
        new = {"id": "b", "product": "Soccer Coach Organizer", "price": "$12", "refunded": False}
        self.assertEqual(s.sales(fetch=lambda after: [old]), 0)
        self.assertEqual(s.sales(fetch=lambda after: [old, new]), 1)
        self.assertEqual(self.sent, ["💰 Gumroad sale: Soccer Coach Organizer, $12."])

    def test_sunday_recap_once_on_sunday_evening(self):
        s = self.scanner()
        parts = {"trading": "Options bots: +240 dollars on 9 closed trades, 6 won (simulated).", "memecoin": None,
                 "grades": ["Lab 3 18/20 (90%)"], "money": "Spent $80 this week, $20 less than usual.",
                 "workouts": [{"title": "Push", "day": "Mon"}], "sales": 1,
                 "canvas": [{"name": "Essay", "course": "English", "due": "2026-10-27", "overdue": False}],
                 "events": []}
        self.now = datetime(2026, 10, 24, 18, 5).timestamp()                # Saturday
        self.assertEqual(s.weekly(gather=lambda now: parts), "")
        self.now = datetime(2026, 10, 25, 18, 5).timestamp()                # Sunday
        self.assertEqual(s.weekly(gather=lambda now: parts), "sent")
        self.assertEqual(s.weekly(gather=lambda now: parts), "")
        text = self.sent[0]
        self.assertTrue(text.startswith("Your week, through Sun Oct 25"))
        for bit in ("📈 Options bots: +240", "💰 1 Gumroad sale this week.", "💵 Spent $80", "🏋️ 1 workout: Push (Mon)",
                    "🎓 Grades: Lab 3 18/20 (90%)", "📚 Due this week (1): Essay (Tue)"):
            self.assertIn(bit, text)


class SourceTests(unittest.TestCase):
    def test_calendar_reads_expanded_occurrences_and_skips_birthdays(self):
        import sqlite3
        from worker import sources
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "Calendar.sqlitedb"
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT);
                CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT, all_day INTEGER);
                CREATE TABLE OccurrenceCache (event_id INTEGER, calendar_id INTEGER,
                    occurrence_date REAL, occurrence_start_date REAL);
                INSERT INTO Calendar VALUES (1, 'School'), (2, 'Birthdays');
                INSERT INTO CalendarItem VALUES (1, 'EE-98 lecture', 0), (2, 'Jacob', 1);""")
            start = datetime(2026, 10, 26, 9, 0)
            for day in range(3):                          # a repeating class: one row per occurrence
                t = (start + timedelta(days=day)).timestamp() - sources.APPLE_EPOCH
                con.execute("INSERT INTO OccurrenceCache VALUES (1, 1, ?, ?)", (t, t))
            con.execute("INSERT INTO OccurrenceCache VALUES (2, 2, ?, NULL)",
                        (start.timestamp() - sources.APPLE_EPOCH,))
            con.commit()
            con.close()
            found = sources.calendar_events(start, start + timedelta(days=2), db=db)
        self.assertEqual([(e["at"], e["title"]) for e in found],
                         [(start, "EE-98 lecture"), (start + timedelta(days=1), "EE-98 lecture")])

    def test_weather_line(self):
        from worker import sources
        def get(url):
            if "geocoding" in url:
                return {"results": [{"name": "San Jose", "latitude": 37.3, "longitude": -121.9}]}
            return {"daily": {"temperature_2m_max": [71.6], "temperature_2m_min": [53.2],
                              "precipitation_probability_max": [20], "weather_code": [2]}}
        self.assertEqual(sources.weather("San Jose", get=get), "San Jose: 72°/53°, partly cloudy, 20% rain")


class LidAwakeTests(unittest.TestCase):
    def _keep(self):
        run = MagicMock(return_value=MagicMock(returncode=0))
        popen = MagicMock()
        popen.return_value.poll.return_value = None
        return KeepAwake(popen=popen, run=run), run

    def test_lid_mode_follows_power_and_turns_itself_off(self):
        k, run = self._keep()
        k.update(True, {"ac": False, "percent": 80}, 40, lid=True)
        self.assertEqual(run.call_args.args[0][-3:], ["-a", "disablesleep", "1"])
        self.assertTrue(k.lid_active)
        k.update(True, {"ac": False, "percent": 80}, 40, lid=True)
        self.assertEqual(run.call_count, 1)                   # not re-run every tick
        k.update(True, {"ac": False, "percent": 30}, 40, lid=True)
        self.assertEqual(run.call_args.args[0][-1], "0")
        self.assertFalse(k.lid_active)

    def test_turning_the_setting_off_restores_sleep(self):
        k, run = self._keep()
        k.update(True, {"ac": True}, 40, lid=True)
        k.update(True, {"ac": True}, 40, lid=False)
        self.assertEqual(run.call_args.args[0][-1], "0")

    def test_missing_sudo_rule_is_retried_every_few_minutes_not_every_tick(self):
        k, run = self._keep()
        run.return_value = MagicMock(returncode=1)
        with patch("worker.runtime.time.monotonic", return_value=1000.0):
            k.update(True, {"ac": True}, 40, lid=True)
            k.update(True, {"ac": True}, 40, lid=True)
        self.assertEqual(run.call_count, 1)
        run.return_value = MagicMock(returncode=0)           # the user ran the one-time setup
        with patch("worker.runtime.time.monotonic", return_value=1400.0):
            k.update(True, {"ac": True}, 40, lid=True)
        self.assertTrue(k.lid_active)

    def test_lid_mode_never_touches_pmset_unless_asked(self):
        k, run = self._keep()
        k.update(True, {"ac": True}, 40)
        run.assert_not_called()

    def test_reads_sleep_disabled(self):
        on = MagicMock(stdout=b"System-wide power settings:\n SleepDisabled\t\t1\nCurrently in use:\n")
        off = MagicMock(stdout=b"System-wide power settings:\nCurrently in use:\n sleep 1\n")
        self.assertTrue(lid_sleep_disabled(run=lambda *a, **k: on))
        self.assertFalse(lid_sleep_disabled(run=lambda *a, **k: off))


class PluginTests(unittest.TestCase):
    def test_mute_and_unmute_a_kind(self):
        from plugins import phone_alerts as plugin
        with patch.object(plugin.pa, "settings", return_value={**pa.DEFAULTS, "off": ["trading"]}), \
                patch.object(plugin, "save_plugin_config") as save:
            out = plugin.run({"action": "mute", "kind": "canvas"})
            self.assertEqual(save.call_args.args, (pa.NS, {"off": ["trading", "canvas"]}))
            self.assertIn("Canvas", out)
            plugin.run({"action": "unmute", "kind": "trading"})
            self.assertEqual(save.call_args.args, (pa.NS, {"off": []}))

    def test_vip_add_and_remove(self):
        from plugins import phone_alerts as plugin
        with patch.object(plugin.pa, "settings", return_value={**pa.DEFAULTS, "vip": ["sjsu.edu"]}), \
                patch.object(plugin, "save_plugin_config") as save:
            plugin.run({"action": "vip_add", "value": "Ms. Lee"})
            self.assertEqual(save.call_args.args, (pa.NS, {"vip": ["sjsu.edu", "Ms. Lee"]}))
            plugin.run({"action": "vip_remove", "value": "SJSU.edu"})
            self.assertEqual(save.call_args.args, (pa.NS, {"vip": []}))
            self.assertIn("isn't on your VIP list", plugin.run({"action": "vip_remove", "value": "nobody"}))

    def test_lid_awake_without_setup_shows_the_one_time_command(self):
        from plugins import phone_alerts as plugin
        player = MagicMock()
        with patch.object(plugin, "_patch_config") as patch_cfg, \
                patch("worker.runtime.LID_RULE", Path("/nonexistent/jarvis-lid-awake")):
            out = plugin.run({"action": "lid_awake", "state": "on"}, player=player)
        patch_cfg.assert_called_once_with(worker_keep_awake=True, worker_keep_awake_lid=True)
        self.assertIn("one-time", out)
        self.assertIn("visudo -cf", player.show_content.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
