import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.plugin_loader import _validate
from plugins import paper_trading as plugin
from trading import __main__ as cli
from trading.broker import BrokerError
from trading.journal import Journal

from trading_fakes import FakeBroker, weekdays_before

SECRET = "SUPER-SECRET-VALUE"
STATE = {"start_equity": 100_000.0, "start_spy": 500.0, "started_at": "2026-10-06",
         "last_seen": {"t": "2026-10-07T10:00:00-04:00", "equity": 100_800.0, "spy": 502.5,
                       "holdings": [{"symbol": "QQQ", "value": 32_000.0}]}}


class PluginCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        patcher = patch.object(plugin, "_journal", lambda: self.journal)
        patcher.start()
        self.addCleanup(patcher.stop)
        keys = patch.object(plugin, "load_keys", return_value=("PKX", SECRET))
        keys.start()
        self.addCleanup(keys.stop)


class LoaderTests(unittest.TestCase):
    def test_jarvis_accepts_the_plugin(self):
        path = Path(plugin.__file__)
        spec = importlib.util.spec_from_file_location("paper_trading_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record = _validate(module, path.name)
        self.assertTrue(record.valid, record.error)
        self.assertEqual(record.name, "paper_trading")
        self.assertEqual(record.settings["namespace"], "alpaca_paper")
        self.assertTrue(all(f["type"] == "password" for f in record.settings["fields"]))

    def test_the_description_rules_out_real_money(self):
        text = plugin.PLUGIN["description"].lower()
        self.assertIn("can not trade real money", text)
        self.assertIn("practice", text)


class StatusTests(PluginCase):
    def test_before_any_trading_it_explains_the_next_step(self):
        self.assertIn("hasn't traded yet", plugin.run({}))

    def test_without_keys_it_points_to_settings(self):
        with patch.object(plugin, "load_keys", return_value=("", "")):
            self.assertIn("Plugin Settings", plugin.run({"action": "status"}))

    def test_status_reads_the_local_record_without_any_network(self):
        self.journal.update_state(**STATE)
        with patch.object(plugin, "make_broker", side_effect=AssertionError("network used")):
            text = plugin.run({"action": "status"})
        self.assertIn("$100,800", text)
        self.assertIn("QQQ", text)
        self.assertIn("isn't running", text)
        self.assertNotIn(SECRET, text)

    def test_status_while_paused_says_so(self):
        self.journal.update_state(**STATE)
        self.journal.pause()
        self.assertIn("paused", plugin.run({}))


class ControlTests(PluginCase):
    def test_pause_and_resume_flip_the_switch(self):
        self.assertIn("Paused", plugin.run({"action": "pause"}))
        self.assertTrue(self.journal.paused())
        plugin.run({"action": "resume"})
        self.assertFalse(self.journal.paused())

    def test_stop_with_nothing_running(self):
        self.assertIn("isn't running", plugin.run({"action": "stop"}))

    def test_unknown_action_lists_what_is_possible(self):
        self.assertIn("start, stop, pause", plugin.run({"action": "buy tesla"}))

    def test_start_launches_a_separate_background_process(self):
        popen = MagicMock()
        with patch.object(plugin.subprocess, "Popen", popen), patch.object(plugin.time, "sleep"), \
             patch.object(Journal, "runner_pid", side_effect=[None, 4242]):
            text = plugin.run({"action": "start"})
        self.assertIn("is running", text)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:], ["-u", "-m", "trading", "run"])
        self.assertEqual(kwargs["cwd"], str(plugin.BASE_DIR))
        if sys.platform != "win32":
            self.assertTrue(kwargs["start_new_session"])

    def test_start_when_already_running_does_not_launch_a_second(self):
        popen = MagicMock()
        with patch.object(plugin.subprocess, "Popen", popen), \
             patch.object(Journal, "runner_pid", return_value=4242):
            self.assertIn("already running", plugin.run({"action": "start"}))
        popen.assert_not_called()

    def test_start_without_keys_does_not_launch(self):
        popen = MagicMock()
        with patch.object(plugin, "load_keys", return_value=("", "")), \
             patch.object(plugin.subprocess, "Popen", popen):
            self.assertIn("Plugin Settings", plugin.run({"action": "start"}))
        popen.assert_not_called()

    def test_broker_errors_are_spoken_plainly_and_never_leak_keys(self):
        with patch.object(plugin, "make_broker", side_effect=BrokerError("Alpaca rejected the paper keys.")):
            text = plugin.run({"action": "plan"})
        self.assertIn("rejected", text)
        self.assertNotIn(SECRET, text)

    def test_plan_reports_without_sending(self):
        broker = FakeBroker(day="2026-10-06", is_open=False)
        with patch.object(plugin, "make_broker", return_value=broker):
            text = plugin.run({"action": "plan"})
        self.assertIn("Nothing was sent", text)
        self.assertEqual(broker.orders, [])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        for target in ("Journal",):
            patcher = patch.object(cli, target, lambda: self.journal)
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

    def test_pause_resume_stop(self):
        self.assertEqual(self.run_cli("pause")[0], 0)
        self.assertTrue(self.journal.paused())
        self.run_cli("resume")
        self.assertFalse(self.journal.paused())
        self.assertIn("No background", self.run_cli("stop")[1])

    def test_check_passes_and_names_each_step(self):
        class Healthy(FakeBroker):
            def daily_bars(self, symbols, start, end=None):
                return {"SPY": [("2026-10-01", 500.0), ("2026-10-02", 501.0)]}
        code, text = self.run_cli("check", broker=Healthy())
        self.assertEqual(code, 0)
        for label in ("keys and account", "market clock", "positions", "price data", "price history"):
            self.assertIn(f"ok    {label}", text)
        self.assertIn("practice money only", text)

    def test_check_failure_is_specific_and_nonzero(self):
        class NoData(FakeBroker):
            def latest_prices(self, symbols):
                raise BrokerError("Alpaca rejected the paper keys.")
        code, text = self.run_cli("check", broker=NoData())
        self.assertEqual(code, 1)
        self.assertIn("FAIL  price data", text)
        self.assertIn("nothing was sent", text.lower())

    def test_missing_keys_print_instructions_not_a_traceback(self):
        with patch.object(cli, "make_broker", side_effect=BrokerError("keys not set up")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("keys not set up", out.getvalue())

    def test_plan_prints_signals_and_orders_but_sends_nothing(self):
        broker = FakeBroker(day="2026-10-06", is_open=False)
        code, text = self.run_cli("plan", broker=broker)
        self.assertEqual(code, 0)
        self.assertIn("Would hold: ", text)
        self.assertIn("would buy QQQ", text)
        self.assertEqual(broker.orders, [])

    def test_backtest_prints_both_columns_and_the_caveat(self):
        class History(FakeBroker):
            def daily_bars(self, symbols, start, end=None):
                stamps = weekdays_before("2026-10-06", 700)
                return {s: [(d, 100.0 * 1.0004 ** i) for i, d in enumerate(stamps)] for s in symbols}
        code, text = self.run_cli("backtest", broker=History())
        self.assertEqual(code, 0)
        self.assertIn("This rule", text)
        self.assertIn("Hold SPY", text)
        self.assertIn("not a forecast", text)

    def test_report_works_offline_from_the_record(self):
        self.journal.update_state(**STATE)
        with patch.object(cli, "make_broker", side_effect=BrokerError("Could not reach Alpaca.")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["report"])
        self.assertEqual(code, 0)
        self.assertIn("last recorded numbers", out.getvalue())
        self.assertIn("$100,800", out.getvalue())


if __name__ == "__main__":
    unittest.main()
