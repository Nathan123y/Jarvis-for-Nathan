import os
import unittest
from unittest.mock import patch

from trading import credentials
from trading.broker import BrokerError
from trading.day import report as reports

PAPER_ID = "PK" + "Z9Y8X7W6V5U4T3S2R1"
SECRET = "x9Y8w7V6u5T4s3R2q1P0o9N8m7L6k5J4i3H2g1F0"


class KeyTests(unittest.TestCase):
    def test_environment_variables_win_and_are_named(self):
        env = {"ALPACA_PAPER_KEY_ID": PAPER_ID, "ALPACA_PAPER_SECRET_KEY": SECRET}
        with patch.dict(os.environ, env):
            self.assertEqual(credentials.load_keys(), (PAPER_ID, SECRET))
            self.assertIn("environment variables", " ".join(credentials.key_report()))

    def test_plugin_settings_are_read_from_one_namespace(self):
        seen = []
        with patch.dict(os.environ, {}, clear=True), \
             patch("memory.config_manager.get_plugin_config", side_effect=lambda ns: seen.append(ns) or {}):
            credentials.load_keys()
        self.assertEqual(seen, ["alpaca_paper"])

    def test_the_plugin_settings_section_uses_that_same_namespace(self):
        from plugins import day_trading
        self.assertEqual(day_trading.PLUGIN_SETTINGS["namespace"], credentials.NAMESPACE)

    def test_missing_keys_say_what_to_do_and_what_balance_is_needed(self):
        with patch.object(credentials, "_read", return_value=("Jarvis Plugin Settings", "", "")):
            with self.assertRaises(BrokerError) as caught:
                credentials.make_broker()
        text = str(caught.exception)
        self.assertIn("Alpaca paper day trading", text)
        self.assertIn("$25,000", text)

    def test_present_keys_build_a_paper_only_broker(self):
        with patch.object(credentials, "_read", return_value=("Jarvis Plugin Settings", PAPER_ID, SECRET)):
            self.assertIsNotNone(credentials.make_broker())

    def test_no_weekly_trader_code_is_left_behind(self):
        for name in ("shares_weekly_account", "SAME_ACCOUNT", "UNVERIFIED", "MISSING_DAY", "DAY_NAMESPACE"):
            self.assertFalse(hasattr(credentials, name), name)


STATE = {"start_equity": 100_000.0, "start_spy": 500.0, "started_at": "2026-10-05"}


def trade(day, symbol, pnl, bought=25_000.0, exit="time"):
    return {"day": day, "symbol": symbol, "pnl": pnl, "bought": bought, "sold": bought + pnl, "exit": exit}


class ReportTests(unittest.TestCase):
    def build(self, days=3, equity=100_400.0, spy=502.0, trades=None, **kw):
        snaps = [{"date": f"2026-10-{5 + i:02d}", "equity": 100_000 + 100 * i, "spy": 500 + i} for i in range(days)]
        return reports.build({**STATE, **kw.pop("state", {})}, snaps, trades or [],
                             account={"equity": equity}, positions={}, spy_price=spy, **kw)

    def test_not_started_says_how_to_start(self):
        report = reports.build({}, [], [])
        self.assertFalse(report["started"])
        self.assertIn("python3 -m trading.day run", reports.render(report))
        self.assertIn("hasn't started", reports.spoken(report))

    def test_numbers_costs_and_the_gap_to_spy(self):
        trades = [trade("2026-10-05", "SPY", 300.0), trade("2026-10-05", "QQQ", -100.0, exit="stop"),
                  trade("2026-10-06", "SPY", 200.0)]
        report = self.build(trades=trades)
        self.assertAlmostEqual(report["return"], 0.004)
        self.assertAlmostEqual(report["spy_return"], 0.004)
        self.assertEqual((report["trades"], report["stops"]), (3, 1))
        self.assertAlmostEqual(report["win_rate"], 2 / 3)
        self.assertAlmostEqual(report["profit_factor"], 5.0)
        expected_costs = sum((t["bought"] + t["sold"]) * 2.0 / 10_000 for t in trades)
        self.assertAlmostEqual(report["costs"], expected_costs)
        self.assertAlmostEqual(report["return_after_costs"], (100_400 - expected_costs) / 100_000 - 1)

    def test_a_repeated_result_is_counted_once(self):
        report = self.build(trades=[trade("2026-10-05", "SPY", 300.0)] * 3)
        self.assertEqual(report["trades"], 1)

    def test_a_short_record_is_called_too_early(self):
        self.assertIn("Too early", self.build()["verdict"])
        self.assertIn("3 trading days", self.build()["verdict"])

    def test_a_long_record_is_judged_after_costs(self):
        ahead = self.build(days=61, equity=110_000.0, spy=505.0)
        self.assertIn("Ahead of simply holding SPY", ahead["verdict"])
        behind = self.build(days=61, equity=99_000.0, spy=505.0)
        self.assertIn("Behind simply holding SPY", behind["verdict"])
        self.assertIn("after estimated costs", behind["verdict"])

    def test_render_and_spoken_show_both_the_raw_and_after_cost_numbers(self):
        report = self.build(trades=[trade("2026-10-05", "SPY", 300.0)], state={"paused": True})
        text = reports.render(report)
        self.assertIn("After est. costs", text)
        self.assertIn("PAUSED", text)
        self.assertIn("after estimated costs", reports.spoken(report))
        self.assertIn("1 trade.", reports.spoken(report))

    def test_recorded_numbers_work_offline(self):
        state = {**STATE, "last_seen": {"t": "2026-10-06T10:00:00-04:00", "equity": 100_250.0,
                                         "holdings": [{"symbol": "SPY", "value": 25_000.0}]}}
        snaps = [{"date": "2026-10-05", "equity": 100_100.0, "spy": 501.0}]
        report = reports.build(state, snaps, [])
        self.assertEqual(report["source"], "recorded")
        self.assertEqual(report["holding"], ["SPY"])
        self.assertAlmostEqual(report["spy_return"], 0.002)
        self.assertIn("last recorded", reports.spoken(report))


class ReportHelperTests(unittest.TestCase):
    def test_latest_snapshot_per_day_wins(self):
        events = [{"date": "d1", "equity": 1}, {"date": "d1", "equity": 2}, {"date": "d2", "equity": 3}, {"equity": 9}]
        self.assertEqual([e["equity"] for e in reports.latest_per_day(events)], [2, 3])

    def test_money_and_percent_formatting(self):
        self.assertEqual(reports._usd(100_400.4), "$100,400")
        self.assertEqual(reports._usd(None), "n/a")
        self.assertEqual(reports._pct(0.004), "+0.40%")
        self.assertEqual(reports._pct(-0.1, signed=False), "-10.0%")
        self.assertEqual(reports._pct(None), "n/a")

    def test_the_sample_size_before_a_verdict_is_sixty_days(self):
        self.assertEqual(reports.MIN_TRADING_DAYS, 60)


if __name__ == "__main__":
    unittest.main()
