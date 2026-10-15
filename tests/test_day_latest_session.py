"""The day trader's status opens with what happened in the latest session."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import day_trading


class FakeJournal:
    def __init__(self, events):
        self._events = events

    def events(self, kind=None):
        return [e for e in self._events if kind is None or e.get("kind") == kind]


class LatestSessionTests(unittest.TestCase):
    def test_a_finished_trade(self):
        j = FakeJournal([
            {"kind": "plan", "day": "2026-10-15", "stand_aside": False},
            {"kind": "trade_result", "day": "2026-10-15", "symbol": "DIA", "qty": 19.0,
             "bought": 9862.81, "sold": 9910.55, "pnl": 47.74},
        ])
        text = day_trading._latest_session(j)
        self.assertIn("bought 19 DIA for $9,863 and sold for $9,911, +$47.74", text)

    def test_no_breakout_explains_why(self):
        j = FakeJournal([
            {"kind": "trade_result", "day": "2026-10-13", "symbol": "SPY", "qty": 1, "bought": 1, "sold": 1, "pnl": 0},
            {"kind": "decision", "day": "2026-10-14", "symbol": "AMD", "status": "late"},
            {"kind": "decision", "day": "2026-10-14", "symbol": "XLE", "status": "late"},
        ])
        text = day_trading._latest_session(j)
        self.assertIn("bought nothing", text)
        self.assertIn("AMD, XLE", text)
        self.assertIn("2 pm", text)

    def test_nothing_recorded(self):
        self.assertEqual(day_trading._latest_session(FakeJournal([])), "")


if __name__ == "__main__":
    unittest.main()
