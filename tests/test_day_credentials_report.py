import os
import unittest
from unittest.mock import patch

from trading import credentials
from trading.broker import BrokerError
from trading.day import report as reports

WEEKLY_ID = "PK" + "A1B2C3D4E5F6G7H8I9"
DAY_ID = "PK" + "Z9Y8X7W6V5U4T3S2R1"
SECRET = "x9Y8w7V6u5T4s3R2q1P0o9N8m7L6k5J4i3H2g1F0"


def reader(weekly=(WEEKLY_ID, SECRET), day=(DAY_ID, SECRET)):
    table = {"weekly": ("Jarvis Plugin Settings", *weekly), "day": ("Jarvis Plugin Settings", *day)}
    return lambda profile="weekly": table[profile]


class DayKeyTests(unittest.TestCase):
    def test_the_day_trader_reads_its_own_environment_variables(self):
        env = {"ALPACA_PAPER_DAY_KEY_ID": DAY_ID, "ALPACA_PAPER_DAY_SECRET_KEY": SECRET,
               "ALPACA_PAPER_KEY_ID": WEEKLY_ID, "ALPACA_PAPER_SECRET_KEY": SECRET}
        with patch.dict(os.environ, env):
            self.assertEqual(credentials.load_keys("day"), (DAY_ID, SECRET))
            self.assertEqual(credentials.load_keys(), (WEEKLY_ID, SECRET))
            self.assertIn("environment variables", " ".join(credentials.key_report("day")))

    def test_plugin_settings_use_a_separate_namespace(self):
        seen = []
        with patch.dict(os.environ, {}, clear=True), \
             patch("memory.config_manager.get_plugin_config", side_effect=lambda ns: seen.append(ns) or {}):
            credentials.load_keys("day")
            credentials.load_keys("weekly")
        self.assertEqual(seen, ["alpaca_paper_day", "alpaca_paper"])

    def test_missing_day_keys_explain_the_second_account(self):
        with patch.object(credentials, "_read", reader(day=("", ""))):
            with self.assertRaises(BrokerError) as caught:
                credentials.make_broker("day")
        self.assertIn("second", str(caught.exception))
        self.assertIn("Alpaca paper day trading", str(caught.exception))

    def test_the_weekly_keys_are_refused_for_the_day_trader(self):
        with patch.object(credentials, "_read", reader(day=(WEEKLY_ID, SECRET))):
            with self.assertRaises(BrokerError) as caught:
                credentials.make_broker("day")
            self.assertIn("same keys", str(caught.exception))
            self.assertIn("same keys", " ".join(credentials.key_report("day")))

    def test_two_different_key_pairs_build_a_broker(self):
        with patch.object(credentials, "_read", reader()):
            self.assertIsNotNone(credentials.make_broker("day"))

    def test_the_weekly_trader_is_unaffected(self):
        with patch.object(credentials, "_read", reader(weekly=(WEEKLY_ID, SECRET), day=("", ""))):
            self.assertIsNotNone(credentials.make_broker())


class FakeAccount:
    def __init__(self, key, secret, ident=None, fail=False):
        self.ident, self.fail = ident, fail

    def account(self):
        if self.fail:
            raise BrokerError("Could not reach Alpaca.")
        return {"account_id": self.ident}


class SameAccountTests(unittest.TestCase):
    def check(self, day_id, weekly_id, weekly_keys=(WEEKLY_ID, SECRET), weekly_fails=False, day_fails=False):
        day = FakeAccount(DAY_ID, SECRET, day_id, day_fails)
        with patch.object(credentials, "_read", reader(weekly=weekly_keys)), \
             patch.object(credentials, "AlpacaPaper", lambda k, s: FakeAccount(k, s, weekly_id, weekly_fails)):
            return credentials.shares_weekly_account(day)

    def test_the_same_account_behind_different_keys_is_caught(self):
        self.assertIs(self.check("acct-1", "acct-1"), True)

    def test_two_different_accounts_are_fine(self):
        self.assertIs(self.check("acct-1", "acct-2"), False)

    def test_no_weekly_keys_means_nothing_to_clash_with(self):
        self.assertIs(self.check("acct-1", "acct-1", weekly_keys=("", "")), False)

    def test_an_unreadable_weekly_side_is_unknown_not_fine(self):
        self.assertIsNone(self.check("acct-1", "acct-1", weekly_fails=True))
        self.assertIsNone(self.check("acct-1", ""))
        self.assertIsNone(self.check("", "acct-1"))

    def test_a_failure_on_the_day_traders_own_side_is_raised(self):
        with self.assertRaises(BrokerError):
            self.check("acct-1", "acct-2", day_fails=True)


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


if __name__ == "__main__":
    unittest.main()
