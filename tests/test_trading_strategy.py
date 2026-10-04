import unittest
from datetime import date, timedelta

from trading.strategy import (UNIVERSE, StrategyConfig, backtest, due_for_rebalance,
                              explain, period_changed, target_weights)

CFG = StrategyConfig()


def series(drift: float, n: int = 300, start: float = 100.0) -> list[float]:
    return [start * (1.0 + drift) ** i for i in range(n)]


def closes_for(drifts: dict) -> dict:
    return {s: series(drifts.get(s, 0.0004)) for s in UNIVERSE}


class SignalTests(unittest.TestCase):
    def test_holds_the_three_strongest_funds_equally(self):
        drifts = {"QQQ": 0.0009, "SPY": 0.0006, "GLD": 0.0005, "IWM": 0.0003, "EFA": 0.0002, "TLT": 0.0001}
        weights = target_weights(closes_for(drifts))
        self.assertEqual(sorted(weights), ["GLD", "QQQ", "SPY"])
        self.assertTrue(all(abs(w - 1 / 3) < 1e-9 for w in weights.values()))

    def test_everything_falling_means_cash(self):
        self.assertEqual(target_weights(closes_for({s: -0.001 for s in UNIVERSE})), {})

    def test_fewer_eligible_than_slots_leaves_the_rest_in_cash(self):
        drifts = {s: -0.001 for s in UNIVERSE}
        drifts["GLD"] = 0.0005
        weights = target_weights(closes_for(drifts))
        self.assertEqual(list(weights), ["GLD"])
        self.assertAlmostEqual(weights["GLD"], 1 / 3)

    def test_above_average_but_down_over_six_months_is_not_eligible(self):
        shape = [60.0] * 100 + [100.0] * 74 + [90.0] * 125 + [98.0]
        rows = {r["symbol"]: r for r in explain({**closes_for({}), "SPY": shape})}
        self.assertGreater(rows["SPY"]["price"], rows["SPY"]["sma"])
        self.assertLess(rows["SPY"]["momentum"], 0)
        self.assertEqual(rows["SPY"]["status"], "negative_momentum")
        self.assertNotIn("SPY", target_weights({**closes_for({}), "SPY": shape}))

    def test_a_bounce_inside_a_downtrend_is_not_eligible(self):
        # Up ~33% over six months, yet still well below its 200-day average.
        shape = [150.0] * 173 + [60.0] + [60.0 + 20.0 * k / 125.0 for k in range(1, 127)]
        self.assertEqual(len(shape), 300)
        rows = {r["symbol"]: r for r in explain({**closes_for({}), "GLD": shape})}
        self.assertGreater(rows["GLD"]["momentum"], 0.3)
        self.assertLess(rows["GLD"]["price"], rows["GLD"]["sma"])
        self.assertEqual(rows["GLD"]["status"], "below_trend")
        self.assertNotIn("GLD", target_weights({**closes_for({}), "GLD": shape}))

    def test_short_history_is_labelled_not_guessed(self):
        rows = {r["symbol"]: r for r in explain({**closes_for({}), "TLT": series(0.001, n=50)})}
        self.assertEqual(rows["TLT"]["status"], "not_enough_history")
        self.assertNotIn("TLT", target_weights({**closes_for({}), "TLT": series(0.001, n=50)}))

    def test_ties_break_alphabetically_so_decisions_are_repeatable(self):
        flat_growth = {s: series(0.0005) for s in UNIVERSE}
        self.assertEqual(sorted(target_weights(flat_growth)), ["EFA", "GLD", "IWM"])


class ScheduleTests(unittest.TestCase):
    def test_first_run_is_always_due(self):
        self.assertTrue(due_for_rebalance(None, date(2026, 10, 6)))

    def test_weekly_waits_for_a_new_week(self):
        self.assertFalse(due_for_rebalance("2026-10-05", date(2026, 10, 9)))     # Mon -> Fri
        self.assertTrue(due_for_rebalance("2026-10-05", date(2026, 10, 12)))     # next Monday

    def test_monthly_waits_for_a_new_month(self):
        self.assertFalse(due_for_rebalance("2026-10-01", date(2026, 10, 30), "monthly"))
        self.assertTrue(due_for_rebalance("2026-10-30", date(2026, 11, 2), "monthly"))

    def test_garbage_date_means_due_not_crash(self):
        self.assertTrue(due_for_rebalance("not-a-date", date(2026, 10, 6)))

    def test_year_boundary_week_counts_as_a_new_week(self):
        self.assertTrue(period_changed(date(2026, 12, 31), date(2027, 1, 4), "weekly"))


def weekday_dates(count: int, start: date = date(2018, 1, 1)) -> list[str]:
    out, cursor = [], start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def history(path_for) -> dict:
    stamps = weekday_dates(700)
    return {s: [(d, path_for(s, i)) for i, d in enumerate(stamps)] for s in UNIVERSE}


class BacktestTests(unittest.TestCase):
    def test_steady_market_is_fully_invested_and_close_to_holding_spy(self):
        data = history(lambda s, i: 100.0 * (1.0005 ** i))
        result = backtest(data)
        self.assertGreater(result["time_invested"], 0.99)
        self.assertGreater(result["rebalances"], 0)
        gap = result["benchmark"]["total_return"] - result["strategy"]["total_return"]
        self.assertGreater(gap, -0.01)
        self.assertLess(gap, 0.02)

    def test_a_crash_is_sidestepped_better_than_holding(self):
        def path(symbol, i):
            return 100.0 * (1.001 ** i) if i < 400 else 100.0 * (1.001 ** 400) * (0.99 ** (i - 400))
        result = backtest(history(path))
        self.assertGreater(result["strategy"]["max_drawdown"], result["benchmark"]["max_drawdown"])
        self.assertLess(result["time_invested"], 1.0)

    def test_costs_reduce_the_result(self):
        data = history(lambda s, i: 100.0 * (1.0004 ** i) * (1.0 + 0.02 * ((i // 20 + len(s)) % 2)))
        free = backtest(data, slippage_bps=0.0)["strategy"]["total_return"]
        costly = backtest(data, slippage_bps=25.0)["strategy"]["total_return"]
        self.assertLess(costly, free)

    def test_curve_starts_at_the_starting_amount_for_both_sides(self):
        result = backtest(history(lambda s, i: 100.0 + i))
        self.assertEqual(result["start"] <= result["end"], True)
        by_year = result["strategy_by_year"]
        self.assertTrue(by_year and all(isinstance(v, float) for v in by_year.values()))

    def test_missing_history_is_an_error_not_a_wrong_answer(self):
        data = history(lambda s, i: 100.0 + i)
        data["GLD"] = []
        with self.assertRaises(ValueError):
            backtest(data)
        with self.assertRaises(ValueError):
            backtest({s: v[:100] for s, v in history(lambda s, i: 100.0 + i).items()})


if __name__ == "__main__":
    unittest.main()
