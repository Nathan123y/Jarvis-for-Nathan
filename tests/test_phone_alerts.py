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

    def test_email_first_pass_is_a_baseline_then_new_mail_is_texted(self):
        service = MagicMock()
        listing = service.users.return_value.messages.return_value.list.return_value.execute
        listing.return_value = {"messages": [{"id": "m1"}]}
        s = self.scanner()
        meta = {"payload": {"headers": [{"name": "From", "value": "Ms. Lee <lee@school.edu>"},
                                        {"name": "Subject", "value": "Midterm moved"}]}}
        with patch("plugins.gmail._fetch", return_value=[(1, "m2", meta)]) as fetch:
            self.assertEqual(s.email(accounts=["school"], service_for=lambda a: service), 0)
            fetch.assert_not_called()
            listing.return_value = {"messages": [{"id": "m2"}, {"id": "m1"}]}
            self.assertEqual(s.email(accounts=["school"], service_for=lambda a: service), 1)
            self.assertEqual(fetch.call_args.args[1], ["m2"])
        self.assertEqual(self.sent, ["📧 School email from Ms. Lee: Midterm moved"])
        query = service.users().messages().list.call_args.kwargs["q"]
        self.assertIn("is:unread", query)
        self.assertIn("-category:promotions", query)

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
