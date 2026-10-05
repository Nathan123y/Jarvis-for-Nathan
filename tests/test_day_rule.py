import unittest
from datetime import date, timedelta

from trading.day import rule
from trading.day.rule import DayConfig

from day_fakes import DAY, day_bars, raw_bars

CFG = DayConfig()


class SessionTests(unittest.TestCase):
    def test_utc_stamps_become_new_york_minutes_in_summer_and_winter(self):
        summer = rule.to_sessions([("2026-10-05T13:30:00Z", 1, 2, 0.5, 1.5)])
        winter = rule.to_sessions([("2026-01-05T14:30:00Z", 1, 2, 0.5, 1.5)])
        self.assertEqual(summer, {"2026-10-05": [(570, 1, 2, 0.5, 1.5)]})
        self.assertEqual(winter, {"2026-01-05": [(570, 1, 2, 0.5, 1.5)]})

    def test_extended_hours_and_bad_stamps_are_dropped(self):
        raw = [("2026-10-05T12:00:00Z", 1, 1, 1, 1),        # 8:00 am, pre-market
               ("2026-10-05T20:00:00Z", 1, 1, 1, 1),        # 4:00 pm, after the bell
               ("garbage", 1, 1, 1, 1),
               ("2026-10-05T13:31:00Z", 1, 1, 1, 1)]
        self.assertEqual([b[0] for b in rule.to_sessions(raw)["2026-10-05"]], [571])

    def test_full_sessions_are_told_from_early_closes_and_holes(self):
        self.assertTrue(rule.is_full_session(day_bars()))
        self.assertFalse(rule.is_full_session(day_bars(last=779)))      # closed at 1:00 pm
        self.assertFalse(rule.is_full_session(day_bars(first=600)))     # missed the open
        self.assertFalse(rule.is_full_session([]))

    def test_round_trip_from_alpaca_style_stamps(self):
        sessions = rule.to_sessions(raw_bars(DAY, day_bars()))
        self.assertEqual(sessions[DAY][0][0], 570)
        self.assertEqual(sessions[DAY][-1][0], 959)


class OpeningRangeTests(unittest.TestCase):
    def test_range_is_the_high_and_low_of_the_first_fifteen_minutes(self):
        self.assertEqual(rule.opening_range(day_bars(), CFG), (100.6, 100.0))

    def test_candles_after_the_range_do_not_widen_it(self):
        bars = day_bars()
        bars[20] = (590, 100.3, 150.0, 50.0, 100.3)
        self.assertEqual(rule.opening_range(bars, CFG), (100.6, 100.0))

    def test_a_patchy_opening_is_refused(self):
        self.assertIsNone(rule.opening_range(day_bars()[:5] + day_bars()[20:], CFG))


class BreakoutTests(unittest.TestCase):
    def test_first_close_above_the_high_after_the_range_wins(self):
        bars = day_bars(breakout=600)
        index = rule.find_breakout(bars, 100.6, CFG)
        self.assertEqual(bars[index][0], 600)

    def test_a_wick_above_the_high_without_a_close_is_not_a_breakout(self):
        bars = day_bars(breakout=None)
        bars[25] = (595, 100.3, 105.0, 100.2, 100.4)
        self.assertIsNone(rule.find_breakout(bars, 100.6, CFG))

    def test_a_close_above_inside_the_opening_range_does_not_count(self):
        bars = day_bars(breakout=None)
        bars[5] = (575, 100.3, 101.0, 100.2, 100.9)
        self.assertIsNone(rule.find_breakout(bars, 100.6, CFG))

    def test_nothing_triggers_from_two_pm(self):
        self.assertIsNone(rule.find_breakout(day_bars(breakout=840), 100.6, CFG))
        self.assertIsNotNone(rule.find_breakout(day_bars(breakout=839), 100.6, CFG))


class SizingTests(unittest.TestCase):
    def test_risk_sets_the_size_when_the_stop_is_close(self):
        sized, why = rule.size_entry(100.0, 100.0, 99.0, 100_000, CFG)    # $1 risk, $250 budget
        self.assertEqual((why, sized["qty"], sized["stop"]), ("ok", 250, 99.0))

    def test_the_quarter_of_the_account_cap_limits_a_close_stop(self):
        sized, _ = rule.size_entry(100.81, 100.6, 100.0, 100_000, CFG)    # risk size 308, cap 247
        self.assertEqual(sized["qty"], 247)

    def test_a_narrow_range_is_refused(self):
        self.assertEqual(rule.size_entry(100.0, 100.02, 100.0, 100_000, CFG)[1], "range_too_narrow")

    def test_a_far_stop_is_refused(self):
        self.assertEqual(rule.size_entry(100.0, 100.0, 97.0, 100_000, CFG)[1], "stop_too_far")

    def test_a_tiny_account_cannot_buy_one_share(self):
        self.assertEqual(rule.size_entry(500.0, 500.0, 495.0, 1_000, CFG)[1], "too_small")

    def test_garbage_in_gives_no_trade(self):
        self.assertIsNone(rule.size_entry(0, 1, 1, 100_000, CFG)[0])
        self.assertIsNone(rule.size_entry(100, 101, 99, 0, CFG)[0])


class SimulationTests(unittest.TestCase):
    def test_a_quiet_climb_is_sold_before_the_close(self):
        result = rule.simulate_day(day_bars(), CFG, 100_000, 0.0)
        trade = result["trade"]
        self.assertEqual((result["status"], trade["reason"], trade["exited"]), ("traded", "time", 945))
        self.assertEqual(trade["entered"], 591)
        self.assertGreater(trade["pnl"], 0)

    def test_the_stop_is_honoured_and_costs_about_the_planned_risk(self):
        result = rule.simulate_day(day_bars(crash_at=700), CFG, 100_000, 0.0)
        trade = result["trade"]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["exited"], 700)
        self.assertLess(trade["pnl"], 0)

    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        trade = rule.simulate_day(day_bars(crash_at=700), CFG, 100_000, 0.0)["trade"]
        self.assertLess(trade["exit"], trade["stop"])

    def test_a_stop_touched_inside_a_candle_fills_at_the_stop(self):
        bars = day_bars()
        bars[200] = (770, 101.5, 101.6, 99.9, 101.4)            # dips through the 100.0 stop and back
        trade = rule.simulate_day(bars, CFG, 100_000, 0.0)["trade"]
        self.assertEqual((trade["reason"], trade["exit"]), ("stop", 100.0))

    def test_costs_are_charged_on_both_fills(self):
        free = rule.simulate_day(day_bars(), CFG, 100_000, 0.0)["trade"]
        costly = rule.simulate_day(day_bars(), CFG, 100_000, 0.0005)["trade"]
        expected = (free["entry"] + free["exit"]) * 0.0005 * free["qty"]
        self.assertAlmostEqual(free["pnl"] - costly["pnl"], expected, delta=expected * 0.01)

    def test_no_breakout_means_no_trade(self):
        result = rule.simulate_day(day_bars(breakout=None), CFG, 100_000, 0.0)
        self.assertEqual(result["status"], "no_breakout")
        self.assertNotIn("trade", result)

    def test_an_early_close_day_is_skipped(self):
        self.assertEqual(rule.simulate_day(day_bars(last=779), CFG, 100_000, 0.0)["status"], "no_session")

    def test_the_buy_is_the_candle_after_the_signal_never_the_signal_candle(self):
        trade = rule.simulate_day(day_bars(breakout=600), CFG, 100_000, 0.0)["trade"]
        self.assertEqual(trade["entered"], 601)


class LiveSignalTests(unittest.TestCase):
    def signal(self, bars, hours, minutes, secs=0, equity=100_000):
        now = hours * 3600 + minutes * 60 + secs
        return rule.live_signal([b for b in bars if b[0] * 60 <= now], CFG, equity, now)

    def test_before_the_range_is_complete_it_waits(self):
        self.assertEqual(self.signal(day_bars(), 9, 44, 59)["status"], "too_early")

    def test_a_breakout_that_just_finished_is_a_buy(self):
        result = self.signal(day_bars(), 9, 51, 5)
        self.assertEqual(result["status"], "enter")
        self.assertEqual((result["qty"], result["stop"]), (247, 100.0))

    def test_a_candle_still_forming_is_not_a_signal(self):
        self.assertEqual(self.signal(day_bars(), 9, 50, 40)["status"], "no_breakout")

    def test_an_old_breakout_is_not_chased(self):
        self.assertEqual(self.signal(day_bars(), 9, 53, 0)["status"], "breakout_stale")
        self.assertEqual(self.signal(day_bars(), 11, 0, 0)["status"], "breakout_stale")

    def test_no_breakout_yet_keeps_waiting_and_then_gives_up_at_two(self):
        quiet = day_bars(breakout=None)
        self.assertEqual(self.signal(quiet, 11, 0, 0)["status"], "no_breakout")
        self.assertEqual(self.signal(quiet, 14, 0, 5)["status"], "late")

    def test_a_missing_opening_range_is_final(self):
        sparse = day_bars()[:4] + day_bars()[20:]
        self.assertEqual(self.signal(sparse, 9, 51, 5)["status"], "no_opening_range")

    def test_sizing_failures_are_reported_not_hidden(self):
        wide = day_bars(rng=(100.0, 104.0), breakout=590)
        self.assertEqual(self.signal(wide, 9, 51, 5)["status"], "stop_too_far")

    def test_live_and_replay_agree_on_the_first_breakout(self):
        bars = day_bars(breakout=620)
        live = self.signal(bars, 10, 21, 5)
        replay = rule.simulate_day(bars, CFG, 100_000, 0.0)
        self.assertEqual(live["status"], "enter")
        self.assertEqual(live["qty"], replay["trade"]["qty"])
        self.assertEqual(live["stop"], replay["trade"]["stop"])
        self.assertEqual(replay["trade"]["entered"], 621)


def sessions_for(days: int, **kwargs):
    stamps = []
    day = date(2025, 1, 6)
    while len(stamps) < days:
        if day.weekday() < 5:
            stamps.append(day.isoformat())
        day += timedelta(days=1)
    spy = {d: day_bars(**kwargs) for d in stamps}
    qqq = {d: day_bars(rng=(200.0, 201.2), **kwargs) for d in stamps}
    return {"SPY": spy, "QQQ": qqq}, stamps


class BacktestTests(unittest.TestCase):
    def test_every_day_trades_both_funds_and_the_numbers_add_up(self):
        sessions, days = sessions_for(70)
        result = rule.backtest(sessions, CFG, slippage_bps=0.0)
        self.assertEqual(result["days"], 70)
        self.assertEqual(result["trades"], 140)
        self.assertEqual(result["days_traded"], 70)
        self.assertEqual(result["time_exits"], 140)
        self.assertEqual(result["win_rate"], 1.0)
        self.assertGreater(result["strategy"]["total_return"], 0)
        self.assertEqual(result["strategy"]["max_drawdown"], 0.0)
        self.assertEqual(set(result["strategy_by_year"]), {2025})

    def test_costs_lower_the_result(self):
        sessions, _ = sessions_for(70)
        free = rule.backtest(sessions, CFG, slippage_bps=0.0)["strategy"]["total_return"]
        costly = rule.backtest(sessions, CFG, slippage_bps=10.0)["strategy"]["total_return"]
        self.assertLess(costly, free)

    def test_losing_days_show_up_as_stops_and_a_drawdown(self):
        sessions, _ = sessions_for(70, crash_at=700)
        result = rule.backtest(sessions, CFG, slippage_bps=0.0)
        self.assertEqual(result["stops"], 140)
        self.assertEqual(result["win_rate"], 0.0)
        self.assertLess(result["strategy"]["total_return"], 0)
        self.assertLess(result["strategy"]["max_drawdown"], 0)
        self.assertEqual(result["profit_factor"], 0.0)

    def test_quiet_days_are_counted_not_traded(self):
        sessions, _ = sessions_for(70, breakout=None)
        result = rule.backtest(sessions, CFG)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["skipped"], {"no_breakout": 140})
        self.assertEqual(result["strategy"]["total_return"], 0.0)

    def test_too_little_history_is_refused_with_a_reason(self):
        sessions, _ = sessions_for(30)
        with self.assertRaises(ValueError) as caught:
            rule.backtest(sessions, CFG)
        self.assertIn("at least", str(caught.exception))
        with self.assertRaises(ValueError):
            rule.backtest({"QQQ": {}}, CFG)

    def test_early_close_days_are_left_out_of_the_comparison_too(self):
        sessions, days = sessions_for(70)
        sessions["SPY"][days[10]] = day_bars(last=779)
        self.assertEqual(rule.backtest(sessions, CFG)["days"], 69)

    def test_adjusted_benchmark_is_used_when_it_covers_every_day(self):
        sessions, days = sessions_for(70)
        daily = [("2025-01-03", 100.0)] + [(d, 100.0 + i) for i, d in enumerate(days, 1)]
        result = rule.backtest(sessions, CFG, benchmark_daily=daily)
        self.assertTrue(result["benchmark_adjusted"])
        self.assertAlmostEqual(result["benchmark"]["total_return"], 0.70, places=6)
        fallback = rule.backtest(sessions, CFG, benchmark_daily=daily[:30])
        self.assertFalse(fallback["benchmark_adjusted"])


class FillSummaryTests(unittest.TestCase):
    def fill(self, symbol, side, qty, price, kind="market", at="2026-10-05T15:00:00Z"):
        return {"symbol": symbol, "side": side, "qty": qty, "price": price, "type": kind, "filled_at": at}

    def test_a_round_trip_becomes_a_profit_and_a_reason(self):
        orders = [self.fill("SPY", "buy", 100, 500.0), self.fill("SPY", "sell", 100, 502.0, "stop")]
        (result,) = rule.summarize_fills(orders, ("SPY", "QQQ"), "2026-10-05")
        self.assertEqual((result["symbol"], round(result["pnl"], 2), result["exit"]), ("SPY", 200.0, "stop"))

    def test_a_market_sale_is_a_time_exit(self):
        orders = [self.fill("QQQ", "buy", 10, 600.0), self.fill("QQQ", "sell", 10, 590.0)]
        (result,) = rule.summarize_fills(orders, ("SPY", "QQQ"), "2026-10-05")
        self.assertEqual((round(result["pnl"], 2), result["exit"]), (-100.0, "time"))

    def test_a_fund_that_is_not_flat_yet_or_did_not_trade_is_left_out(self):
        open_trade = [self.fill("SPY", "buy", 100, 500.0)]
        self.assertEqual(rule.summarize_fills(open_trade, ("SPY", "QQQ"), "2026-10-05"), [])
        self.assertEqual(rule.summarize_fills([], ("SPY", "QQQ"), "2026-10-05"), [])

    def test_yesterdays_fills_do_not_count_today(self):
        orders = [self.fill("SPY", "buy", 100, 500.0, at="2026-10-02T15:00:00Z"),
                  self.fill("SPY", "sell", 100, 501.0, at="2026-10-02T19:00:00Z")]
        self.assertEqual(rule.summarize_fills(orders, ("SPY",), "2026-10-05"), [])


class CurveTests(unittest.TestCase):
    def test_two_years_of_steady_growth_annualise_to_ten_percent(self):
        curve = [("2024-01-01", 100.0), ("2025-01-01", 110.0), ("2026-01-01", 121.0)]
        metrics = rule.curve_metrics(curve)
        self.assertAlmostEqual(metrics["total_return"], 0.21)
        self.assertAlmostEqual(metrics["cagr"], 0.10, places=2)
        self.assertEqual(metrics["max_drawdown"], 0.0)

    def test_the_worst_fall_is_measured_from_the_highest_point_so_far(self):
        curve = [("2025-01-01", 100.0), ("2025-02-01", 120.0), ("2025-03-01", 90.0), ("2025-04-01", 110.0)]
        self.assertAlmostEqual(rule.curve_metrics(curve)["max_drawdown"], -0.25)

    def test_under_half_a_year_reports_the_plain_total_not_an_annualised_number(self):
        curve = [("2025-01-01", 100.0), ("2025-02-01", 110.0)]
        metrics = rule.curve_metrics(curve)
        self.assertEqual(metrics["cagr"], metrics["total_return"])

    def test_returns_are_split_by_calendar_year(self):
        curve = [("2024-12-31", 100.0), ("2025-06-01", 150.0), ("2025-12-31", 120.0), ("2026-03-01", 132.0)]
        years = rule.curve_by_year(curve)
        self.assertAlmostEqual(years[2024], 0.0)
        self.assertAlmostEqual(years[2025], 0.20)
        self.assertAlmostEqual(years[2026], 0.10)


if __name__ == "__main__":
    unittest.main()
