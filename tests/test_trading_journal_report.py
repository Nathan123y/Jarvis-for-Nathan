import os
import tempfile
import unittest
from pathlib import Path

from trading import report
from trading.journal import Journal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name) / "nested")

    def test_events_round_trip_and_filter_by_kind(self):
        self.journal.record("order", symbol="SPY")
        self.journal.record("error", label="x")
        self.assertEqual([e["symbol"] for e in self.journal.events("order")], ["SPY"])
        self.assertEqual(len(self.journal.events()), 2)

    def test_damaged_lines_are_skipped_not_fatal(self):
        self.journal.record("order", symbol="SPY")
        with open(self.journal.events_path, "a") as handle:
            handle.write("{not json\n")
        self.assertEqual(len(self.journal.events()), 1)

    def test_state_updates_merge(self):
        self.journal.update_state(a=1)
        self.journal.update_state(b=2)
        self.assertEqual(self.journal.state(), {"a": 1, "b": 2})

    def test_order_tally_resets_each_day(self):
        self.journal.note_order("2026-10-06", "SPY", "buy")
        self.journal.note_order("2026-10-06", "QQQ", "sell")
        self.assertEqual(self.journal.orders_today("2026-10-06"), (2, frozenset({"SPY"})))
        self.assertEqual(self.journal.orders_today("2026-10-07"), (0, frozenset()))

    def test_pause_switch(self):
        self.assertFalse(self.journal.paused())
        self.journal.pause()
        self.assertTrue(self.journal.paused())
        self.journal.resume()
        self.journal.resume()                       # harmless twice
        self.assertFalse(self.journal.paused())

    def test_only_one_runner_at_a_time(self):
        self.assertTrue(self.journal.claim_runner())
        self.assertEqual(self.journal.runner_pid(), os.getpid())
        self.journal.release_runner()
        self.assertIsNone(self.journal.runner_pid())

    def test_stale_or_foreign_pid_file_is_not_a_runner(self):
        self.journal.dir.mkdir(parents=True)
        self.journal.pid_path.write_text("999999")
        self.assertIsNone(self.journal.runner_pid())
        self.assertTrue(self.journal.claim_runner())
        self.journal.pid_path.write_text("garbage")
        self.assertIsNone(self.journal.runner_pid())

    def test_stopping_with_nothing_running_is_a_clean_no(self):
        self.assertFalse(self.journal.stop_runner())

    def test_unwritable_folder_never_breaks_trading(self):
        blocked = Journal(Path(self._tmp.name) / "file")
        Path(self._tmp.name, "file").write_text("i am a file, not a folder")
        blocked.record("order", symbol="SPY")        # must not raise
        blocked.update_state(a=1)


STATE = {"start_equity": 100_000.0, "start_spy": 500.0, "started_at": "2026-10-06"}


class ReportTests(unittest.TestCase):
    def live(self, equity, spy, holdings=None, **state):
        account = {"equity": equity}
        positions = {s: {"market_value": v} for s, v in (holdings or {}).items()}
        return report.build({**STATE, **state}, [], account=account, positions=positions, spy_price=spy)

    def test_not_started_says_so(self):
        built = report.build({}, [])
        self.assertFalse(built["started"])
        self.assertIn("not started", report.render(built).lower())

    def test_returns_are_measured_against_spy_from_the_same_start(self):
        built = self.live(102_000.0, 505.0, {"SPY": 40_000.0, "QQQ": 40_000.0})
        self.assertAlmostEqual(built["return"], 0.02)
        self.assertAlmostEqual(built["spy_return"], 0.01)
        self.assertAlmostEqual(built["vs_spy"], 0.01)
        self.assertAlmostEqual(built["cash_pct"], 22_000 / 102_000)

    def test_a_short_record_is_called_too_early_whatever_the_result(self):
        winning = self.live(130_000.0, 500.0)
        self.assertIn("Too early", winning["verdict"])
        self.assertIn("Too early", self.live(80_000.0, 500.0)["verdict"])

    def test_a_long_record_says_ahead_or_behind_without_overclaiming(self):
        days = [{"date": f"2026-{m:02d}-{d:02d}", "equity": 100_000.0 + i}
                for i, (m, d) in enumerate((m, d) for m in range(10, 13) for d in range(1, 28))]
        ahead = report.build(STATE, days, account={"equity": 110_000.0}, positions={}, spy_price=505.0)
        self.assertIn("Ahead", ahead["verdict"])
        self.assertIn("short sample", ahead["verdict"])
        behind = report.build(STATE, days, account={"equity": 101_000.0}, positions={}, spy_price=550.0)
        self.assertIn("Behind", behind["verdict"])

    def test_worst_dip_comes_from_the_recorded_days(self):
        snaps = [{"date": "d1", "equity": 105_000.0}, {"date": "d2", "equity": 94_500.0}]
        built = report.build(STATE, snaps, account={"equity": 100_000.0}, positions={}, spy_price=500.0)
        self.assertAlmostEqual(built["max_drawdown"], -0.10)

    def test_recorded_numbers_are_used_when_offline(self):
        state = {**STATE, "last_seen": {"t": "2026-10-07T10:00:00-04:00", "equity": 101_000.0, "spy": 502.0,
                                        "holdings": [{"symbol": "GLD", "value": 30_000.0}]}}
        built = report.build(state, [])
        self.assertEqual(built["source"], "recorded")
        self.assertAlmostEqual(built["return"], 0.01)
        self.assertIn("last recorded", report.spoken(built))

    def test_spoken_summary_is_short_and_carries_the_numbers(self):
        spoken = report.spoken(self.live(100_400.0, 501.0, {"SPY": 30_000.0}, paused=True))
        self.assertIn("$100,400", spoken)
        self.assertIn("+0.40%", spoken)
        self.assertIn("paused", spoken)
        self.assertLess(len(spoken), 600)

    def test_latest_snapshot_per_day_wins(self):
        events = [{"date": "d1", "equity": 1}, {"date": "d1", "equity": 2}, {"date": "d2", "equity": 3}]
        self.assertEqual([e["equity"] for e in report.latest_per_day(events)], [2, 3])


if __name__ == "__main__":
    unittest.main()
