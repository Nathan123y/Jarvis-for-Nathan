import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.plugin_loader import _validate
from plugins import day_trading as plugin
from trading.day import __main__ as cli
from trading.broker import BrokerError
from trading.journal import Journal

from day_fakes import DAY, DayFakeBroker, history_raw, raw_bars, day_bars

SECRET = "SUPER-SECRET-DAY-VALUE"
STATE = {"start_equity": 100_000.0, "start_spy": 500.0, "started_at": "2026-10-05",
         "last_seen": {"t": "2026-10-06T10:00:00-04:00", "equity": 100_300.0, "holdings": []}}


class PluginCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        for target, value in (("_journal", lambda: self.journal),
                              ("load_keys", lambda profile="day": ("PKX", SECRET))):
            patcher = patch.object(plugin, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class LoaderTests(unittest.TestCase):
    def test_jarvis_accepts_the_plugin_and_both_traders_load_side_by_side(self):
        records = {}
        for name in ("day_trading", "paper_trading"):
            path = Path(plugin.__file__).with_name(f"{name}.py")
            spec = importlib.util.spec_from_file_location(f"{name}_probe", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            record = _validate(module, path.name)
            self.assertTrue(record.valid, record.error)
            records[name] = record
        self.assertEqual(records["day_trading"].settings["namespace"], "alpaca_paper_day")
        self.assertNotEqual(records["day_trading"].settings["namespace"], records["paper_trading"].settings["namespace"])
        self.assertTrue(all(f["type"] == "password" for f in records["day_trading"].settings["fields"]))

    def test_the_description_rules_out_real_money_and_separates_it_from_the_weekly_tool(self):
        text = plugin.PLUGIN["description"].lower()
        self.assertIn("can not trade real money", text)
        self.assertIn("practice", text)
        self.assertIn("paper_trading", text)
        from plugins import paper_trading
        self.assertIn("day_trading", paper_trading.PLUGIN["description"])


class StatusAndControlTests(PluginCase):
    def test_before_any_trading_it_explains_the_next_step(self):
        with patch.object(Journal, "runner_pid", return_value=None):
            text = plugin.run({})
        self.assertIn("hasn't traded yet and isn't running", text)
        with patch.object(Journal, "runner_pid", return_value=4242):
            self.assertIn("running and waiting for the market", plugin.run({}))

    def test_without_keys_it_explains_the_second_account(self):
        with patch.object(plugin, "load_keys", lambda profile="day": ("", "")):
            text = plugin.run({"action": "status"})
        self.assertIn("second free Alpaca paper account", text)
        self.assertNotIn(SECRET, text)

    def test_status_reads_the_local_record_without_any_network(self):
        self.journal.update_state(**STATE)
        self.journal.record("snapshot", date="2026-10-05", equity=100_300.0, spy=501.0)
        with patch.object(plugin, "make_broker", side_effect=AssertionError("network used")), \
             patch.object(Journal, "runner_pid", return_value=None):
            text = plugin.run({"action": "status"})
        self.assertIn("$100,300", text)
        self.assertIn("isn't running", text)
        self.assertNotIn(SECRET, text)

    def test_pause_resume_stop(self):
        self.assertIn("still sells", plugin.run({"action": "pause"}))
        self.assertTrue(self.journal.paused())
        plugin.run({"action": "resume"})
        self.assertFalse(self.journal.paused())
        self.assertIn("isn't running", plugin.run({"action": "stop"}))

    def test_unknown_action_lists_what_is_possible(self):
        self.assertIn("start, stop, pause", plugin.run({"action": "buy tesla"}))

    def test_start_launches_the_day_trader_as_a_separate_background_process(self):
        popen = MagicMock()
        with patch.object(plugin.subprocess, "Popen", popen), patch.object(plugin.time, "sleep"), \
             patch.object(Journal, "runner_pid", side_effect=[None, 4242]):
            text = plugin.run({"action": "start"})
        self.assertIn("is running", text)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:], ["-u", "-m", "trading.day", "run"])
        self.assertEqual(kwargs["cwd"], str(plugin.BASE_DIR))
        if sys.platform != "win32":
            self.assertTrue(kwargs["start_new_session"])

    def test_start_when_already_running_or_without_keys_launches_nothing(self):
        popen = MagicMock()
        with patch.object(plugin.subprocess, "Popen", popen), patch.object(Journal, "runner_pid", return_value=4242):
            self.assertIn("already running", plugin.run({"action": "start"}))
        with patch.object(plugin.subprocess, "Popen", popen), \
             patch.object(plugin, "load_keys", lambda profile="day": ("", "")):
            self.assertIn("Plugin Settings", plugin.run({"action": "start"}))
        popen.assert_not_called()

    def test_broker_errors_are_spoken_plainly_and_never_leak_keys(self):
        with patch.object(plugin, "make_broker", side_effect=BrokerError("Alpaca rejected the paper keys.")):
            text = plugin.run({"action": "plan"})
        self.assertIn("rejected", text)
        self.assertNotIn(SECRET, text)

    def test_plan_describes_the_latest_session_and_sends_nothing(self):
        class Full(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                return {s: raw_bars(DAY, self.bars[s]) for s in symbols}
        broker = Full()
        with patch.object(plugin, "make_broker", return_value=broker):
            text = plugin.run({"action": "plan"})
        self.assertIn(DAY, text)
        self.assertIn("SPY: bought", text)
        self.assertIn("Nothing was sent", text)
        self.assertEqual(broker.entry_orders, [])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        for target, value in (("day_journal", lambda: self.journal),
                              ("key_report", lambda profile="day": ["Keys read from: somewhere."]),
                              ("shares_weekly_account", lambda broker: False)):
            patcher = patch.object(cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv, broker=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            if broker is not None:
                with patch.object(cli, "make_broker", return_value=broker):
                    code = cli.main(list(argv))
            else:
                code = cli.main(list(argv))
        return code, out.getvalue()

    def test_check_passes_and_names_each_step(self):
        code, text = self.run_cli("check", broker=DayFakeBroker())
        self.assertEqual(code, 0)
        for label in ("keys and account", "its own account", "market clock", "positions", "minute prices"):
            self.assertIn(f"ok    {label}", text)
        self.assertIn("practice money only", text)
        self.assertLess(text.index("keys  Keys read from"), text.index("ok    keys and account"))

    def test_check_fails_when_the_balance_is_too_small_to_day_trade(self):
        code, text = self.run_cli("check", broker=DayFakeBroker(equity=10_000.0))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  keys and account", text)
        self.assertIn("$10,000", text)
        self.assertIn("$25,000", text)
        self.assertIn("create a new one", text)
        code, _ = self.run_cli("check", broker=DayFakeBroker(equity=25_000.0))
        self.assertEqual(code, 0, "exactly the minimum is allowed, the same line the runner uses")

    def test_check_fails_on_the_weekly_traders_account(self):
        with patch.object(cli, "shares_weekly_account", lambda broker: True):
            code, text = self.run_cli("check", broker=DayFakeBroker())
        self.assertEqual(code, 1)
        self.assertIn("FAIL  its own account", text)

    def test_check_fails_when_the_account_holds_other_funds(self):
        code, text = self.run_cli("check", broker=DayFakeBroker(held={"GLD": 5}))
        self.assertEqual(code, 1)
        self.assertIn("GLD", text)
        self.assertIn("fresh paper account", text)

    def test_check_and_run_refuse_when_the_accounts_cannot_be_compared(self):
        with patch.object(cli, "shares_weekly_account", lambda broker: None):
            code, text = self.run_cli("check", broker=DayFakeBroker())
            self.assertEqual(code, 1)
            self.assertIn("FAIL  its own account", text)
            self.assertIn("Couldn't confirm", text)
            broker = DayFakeBroker()
            code, text = self.run_cli("run", "--once", broker=broker)
        self.assertEqual(code, 1)
        self.assertIn("won't start", text)
        self.assertEqual(broker.entry_orders, [])

    def test_run_refuses_the_weekly_traders_account_and_never_starts(self):
        broker = DayFakeBroker()
        with patch.object(cli, "shares_weekly_account", lambda b: True):
            code, text = self.run_cli("run", "--once", broker=broker)
        self.assertEqual(code, 1)
        self.assertIn("second", text)
        self.assertIsNone(self.journal.runner_pid())
        self.assertEqual(broker.entry_orders, [])

    def test_run_once_does_one_pass_and_releases_the_lock(self):
        broker = DayFakeBroker()
        code, text = self.run_cli("run", "--once", broker=broker)
        self.assertEqual(code, 0)
        self.assertIn("bought", text)
        self.assertIsNone(self.journal.runner_pid())
        self.assertTrue(broker.entry_orders)

    def test_run_once_leaves_what_it_bought_alone(self):
        broker = DayFakeBroker()
        self.run_cli("run", "--once", broker=broker)
        self.assertTrue(broker.held)
        self.assertEqual(broker.close_calls, [])

    def test_a_stopped_runner_sells_what_it_holds_and_says_so(self):
        broker = DayFakeBroker(held={"SPY": 5})
        with patch.object(cli.DayRunner, "run_forever", lambda self, stop=None: None):
            code, text = self.run_cli("run", broker=broker)
        self.assertEqual(code, 0)
        self.assertEqual(broker.held, {})
        self.assertIn("sold them first", text)
        self.assertIsNone(self.journal.runner_pid())

    def test_a_second_runner_is_refused(self):
        self.journal.claim_runner()
        self.addCleanup(self.journal.release_runner)
        with patch.object(Journal, "runner_pid", return_value=99999):
            code, text = self.run_cli("run", "--once", broker=DayFakeBroker())
        self.assertEqual(code, 1)
        self.assertIn("already running", text)

    def test_plan_shows_the_latest_full_session(self):
        class Full(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                return {s: raw_bars(DAY, self.bars[s]) for s in symbols}
        code, text = self.run_cli("plan", broker=Full())
        self.assertEqual(code, 0)
        self.assertIn(DAY, text)
        self.assertIn("shares in at", text)
        self.assertIn("Nothing was sent", text)

    def test_plan_with_no_session_says_so(self):
        class Empty(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                return {s: [] for s in symbols}
        code, text = self.run_cli("plan", broker=Empty())
        self.assertEqual(code, 1)
        self.assertIn("No full trading session", text)

    def test_backtest_prints_both_columns_the_cost_check_and_the_caveat(self):
        class History(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                raw = history_raw(70)
                return {s: raw[s] for s in symbols}
        code, text = self.run_cli("backtest", broker=History())
        self.assertEqual(code, 0)
        for expected in ("This rule", "Hold SPY", "Trades: 140", "Cost check", "not a forecast",
                         "IEX exchange", "without dividends"):
            self.assertIn(expected, text)

    def test_backtest_asks_for_yesterday_as_the_end_on_the_sip_feed(self):
        seen = {}

        class Recorder(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                seen.update(feed=feed, end=end)
                raw = history_raw(70)
                return {s: raw[s] for s in symbols}
        code, text = self.run_cli("backtest", "--feed", "sip", broker=Recorder())
        self.assertEqual(code, 0)
        self.assertEqual(seen["feed"], "sip")
        self.assertRegex(seen["end"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("consolidated market tape", text)

    def test_backtest_with_too_little_history_explains(self):
        class Short(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                raw = history_raw(10)
                return {s: raw[s] for s in symbols}
        code, text = self.run_cli("backtest", broker=Short())
        self.assertEqual(code, 1)
        self.assertIn("at least", text)

    def test_pause_resume_stop(self):
        self.assertEqual(self.run_cli("pause")[0], 0)
        self.assertTrue(self.journal.paused())
        self.run_cli("resume")
        self.assertFalse(self.journal.paused())
        self.assertIn("No background", self.run_cli("stop")[1])

    def test_report_before_the_first_trade_says_if_the_trader_is_running(self):
        for pid, expected in ((4242, "running and waiting"), (None, "not running")):
            with patch.object(Journal, "runner_pid", return_value=pid), \
                 patch.object(cli, "make_broker", side_effect=BrokerError("offline")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.main(["report"])
            self.assertIn(expected, out.getvalue())

    def test_report_works_offline_from_the_record(self):
        self.journal.update_state(**STATE)
        self.journal.record("snapshot", date="2026-10-05", equity=100_300.0, spy=501.0)
        with patch.object(cli, "make_broker", side_effect=BrokerError("Could not reach Alpaca.")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["report"])
        self.assertEqual(code, 0)
        self.assertIn("last recorded numbers", out.getvalue())
        self.assertIn("$100,300", out.getvalue())

    def test_missing_keys_print_instructions_not_a_traceback(self):
        with patch.object(cli, "make_broker", side_effect=BrokerError("The day trader's keys are not set up.")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("not set up", out.getvalue())


if __name__ == "__main__":
    unittest.main()
