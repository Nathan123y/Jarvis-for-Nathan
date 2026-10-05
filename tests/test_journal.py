import os
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
