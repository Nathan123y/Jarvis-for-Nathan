"""The fair-value-gap rule (trading.day.fvg): pure functions, the replay, and the live runner path."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trading.broker import PAPER_URL, AlpacaPaper
from trading.day import fvg
from trading.day.fvg import FvgConfig, find_gap, five_minute, levels, live_signal, simulate_day, size
from trading.day.rule import summarize_fills
from trading.day.runner import DayRunner
from trading.journal import Journal

from day_fakes import DayFakeBroker, raw_bars, weekdays
from trading_fakes import FakeResponse, FakeSession

RNG = (101.0, 100.0)                       # opening-range high, low
OPENING = {570: (100.5, 101.0, 100.0, 100.5), 575: (100.5, 100.8, 100.2, 100.5)}
C1 = (100.5, 100.9, 100.3, 100.8)          # 9:40
C2 = (100.8, 101.6, 100.7, 101.5)          # 9:45: closed up, traded through 101
C3 = (101.5, 101.9, 101.2, 101.8)          # 9:50: low 101.2 above c1's high 100.9 -> a gap
# entry 101.20, stop 100.29 (c1 low - 0.01), risk 0.91, target 103.02


def expand(start, o, h, l, c):
    """Five one-minute candles that add up to exactly (o, h, l, c)."""
    return [(start, o, o, o, o), (start + 1, o, h, o, h), (start + 2, h, h, l, l),
            (start + 3, l, max(l, c), l, c), (start + 4, c, c, c, c)]


def session(candles=None, after=(), drop=(), last=959):
    """A full day: five-minute candles (start -> o,h,l,c), then explicit one-minute candles, then flat."""
    candles = {**OPENING, 580: C1, 585: C2, 590: C3} if candles is None else candles
    bars = {}
    for start, ohlc in candles.items():
        for bar in expand(start, *ohlc):
            bars[bar[0]] = bar
    for bar in after:
        bars[bar[0]] = bar
    out, price = [], 100.5
    for m in range(570, last + 1):
        if m in drop:
            continue
        if m in bars:
            out.append(bars[m])
            price = bars[m][4]
        else:
            out.append((m, price, price, price, price))
    return out


def upto(candles, last=590):
    """Only the candles up to c3: the flat test filler after it can legitimately form a later gap."""
    return {k: v for k, v in candles.items() if k <= last}


def candle(start, o, h, l, c):
    return (start, o, h, l, c)


FILL = (596, 101.5, 101.5, 101.1, 101.2)                  # trades a cent through 101.20
TARGET = (598, 101.3, 103.1, 101.3, 103.0)                # a cent through 103.02


class CandleTests(unittest.TestCase):
    def test_candles_are_aligned_to_half_past_nine_and_add_up(self):
        candles = five_minute(session())
        self.assertEqual(candles[580], (580, *C1))
        self.assertEqual(candles[585], (585, *C2))
        self.assertEqual(sorted(candles)[0], 570)

    def test_a_candle_with_fewer_than_three_minutes_is_left_out(self):
        candles = five_minute(session(drop={586, 587, 588}))
        self.assertNotIn(585, candles)
        self.assertIn(585, five_minute(session(drop={586, 587})))

    def test_a_missing_minute_candle_cannot_create_a_gap(self):
        self.assertIsNone(find_gap(five_minute(session(drop={586, 587, 588})), RNG))
        whole = session(drop=set(range(585, 590)))
        self.assertIsNone(find_gap(five_minute(whole), RNG), "c1 and c3 are not neighbours")


class PatternTests(unittest.TestCase):
    def test_the_basic_up_gap_and_its_levels(self):
        gap = find_gap(five_minute(session()), RNG)
        self.assertEqual((gap["direction"], gap["c1"][0], gap["c3"][0], gap["done"]), ("up", 580, 590, 595))
        self.assertEqual(levels(gap), {"entry": 101.2, "stop": 100.29, "target": 103.02, "risk": 0.91})

    def test_a_wick_through_the_level_without_a_gap_does_not_count(self):
        overlap = (101.5, 101.9, 100.85, 101.8)               # c3 dips into c1's range
        self.assertIsNone(find_gap(upto(five_minute(session({**OPENING, 580: C1, 585: C2, 590: overlap}))), RNG))

    def test_c2_must_trade_through_the_level_and_close_in_the_gap_direction(self):
        short_of = (100.8, 100.95, 100.7, 100.9)
        self.assertIsNone(find_gap(upto(five_minute(session({**OPENING, 580: C1, 585: short_of, 590: C3}))), RNG))
        red = (101.5, 101.6, 100.7, 100.95)                    # traded through but closed down
        self.assertIsNone(find_gap(upto(five_minute(session({**OPENING, 580: C1, 585: red, 590: C3}))), RNG))

    def test_c2_starting_before_nine_forty_five_is_rejected(self):
        early = {575: candle(575, *C1), 580: candle(580, *C2), 585: candle(585, *C3)}
        self.assertIsNone(find_gap(early, RNG))
        on_time = {580: candle(580, *C1), 585: candle(585, *C2), 590: candle(590, *C3)}
        self.assertIsNotNone(find_gap(on_time, RNG))

    def test_the_pattern_must_be_complete_by_eleven(self):
        by_eleven = {645: candle(645, *C1), 650: candle(650, *C2), 655: candle(655, *C3)}
        self.assertEqual(find_gap(by_eleven, RNG)["done"], 660)
        too_late = {650: candle(650, *C1), 655: candle(655, *C2), 660: candle(660, *C3)}
        self.assertIsNone(find_gap(too_late, RNG))

    def test_close_beyond_variant_needs_c2_to_close_above_the_level(self):
        wick = (100.8, 101.6, 100.7, 100.95)                   # up candle, closed under 101
        candles = upto(five_minute(session({**OPENING, 580: C1, 585: wick, 590: C3})))
        self.assertIsNotNone(find_gap(candles, RNG))
        self.assertIsNone(find_gap(candles, RNG, FvgConfig(c2_must="close")))

    def test_mid_gap_variant(self):
        gap = find_gap(five_minute(session()), RNG)
        self.assertEqual(levels(gap, FvgConfig(entry_at="mid"))["entry"], 101.05)


BEAR = {**OPENING, 580: (100.5, 100.7, 100.1, 100.2), 585: (100.2, 100.3, 99.4, 99.5),
        590: (99.5, 99.9, 99.2, 99.3),
        630: (99.5, 100.0, 99.4, 99.9), 635: (99.9, 101.5, 99.8, 101.4), 640: (101.4, 101.8, 100.2, 101.7)}


class FirstGapTests(unittest.TestCase):
    def test_a_down_first_gap_stands_aside_even_if_an_up_gap_follows(self):
        self.assertEqual(find_gap(five_minute(session(BEAR)), RNG)["direction"], "down")
        later_only = {k: v for k, v in BEAR.items() if k not in (580, 585, 590)}
        self.assertEqual(find_gap(five_minute(session(later_only)), RNG)["direction"], "up")
        self.assertEqual(simulate_day(session(BEAR), FvgConfig(), 100_000, 0.0)["status"], "bearish_stand_aside")

    def test_the_first_up_gap_wins(self):
        two = {**OPENING, 580: C1, 585: C2, 590: C3,
               630: (101.8, 101.9, 101.5, 101.8), 635: (101.8, 103.0, 101.7, 102.9), 640: (102.9, 103.5, 102.5, 103.4)}
        self.assertEqual(find_gap(five_minute(session(two)), RNG)["c3"][0], 590)


class SizeTests(unittest.TestCase):
    def test_the_smaller_of_the_risk_cap_and_the_fund_cap(self):
        self.assertEqual(size({"entry": 101.2, "risk": 0.91}, 100_000), (247, "ok"))       # 25% of the account
        self.assertEqual(size({"entry": 100.0, "risk": 1.2}, 100_000), (208, "ok"))        # 0.25% at risk
        self.assertEqual(size({"entry": 100.0, "risk": 1.2}, 100_000,
                              FvgConfig(risk_per_trade=0.005, max_position_pct=0.5)), (416, "ok"))

    def test_risk_limits_and_tiny_accounts(self):
        self.assertEqual(size({"entry": 100.0, "risk": 0.04}, 100_000), (None, "risk_too_small"))
        self.assertEqual(size({"entry": 100.0, "risk": 1.6}, 100_000), (None, "stop_too_far"))
        self.assertEqual(size({"entry": 100.0, "risk": 1.0}, 100), (None, "too_small"))

    def test_the_target_is_two_to_one_in_cents(self):
        gap = {"direction": "up", "c1": (0, 50.0, 50.333, 49.987, 50.1), "c3": (0, 50.6, 50.9, 50.555, 50.8)}
        lv = levels(gap)
        self.assertEqual((lv["entry"], lv["stop"]), (50.55, 49.98))  # 50.555 is 50.5549... in binary
        self.assertEqual(lv["target"], 51.69)                        # 50.55 + 2 x 0.57


class ReplayTests(unittest.TestCase):
    def run_day(self, after, cfg=FvgConfig(), slip=0.0):
        return simulate_day(session(after=after), cfg, 100_000, slip)

    def test_fill_then_target_is_plus_two_r(self):
        r = self.run_day([FILL, (597, 101.2, 101.4, 101.2, 101.3), TARGET])
        t = r["trade"]
        self.assertEqual((r["status"], t["reason"], t["qty"], t["entry"], t["exit"]), ("traded", "target", 247, 101.2, 103.02))
        self.assertAlmostEqual(t["r"], 2.0)

    def test_a_limit_touched_but_not_traded_through_does_not_fill(self):
        touch = (596, 101.5, 101.5, 101.2, 101.3)
        self.assertEqual(self.run_day([touch])["status"], "never_filled")
        self.assertEqual(self.run_day([touch], FvgConfig(fill_on="touch"))["status"], "traded")

    def test_a_target_touched_but_not_traded_through_is_not_hit(self):
        r = self.run_day([FILL, (598, 101.3, 103.02, 101.3, 103.0)])
        self.assertEqual(r["trade"]["reason"], "time")

    def test_unfilled_at_eleven_is_cancelled(self):
        late = (660, 101.5, 101.5, 101.0, 101.1)                 # comes back only at 11:00
        self.assertEqual(self.run_day([late])["status"], "never_filled")

    def test_the_fill_bar_can_hit_the_stop_but_not_the_target(self):
        both = (596, 101.5, 103.5, 100.2, 101.0)
        r = self.run_day([both])
        self.assertEqual(r["trade"]["reason"], "stop")
        through = (596, 101.5, 103.5, 101.1, 102.0)              # fills and runs past the target in one bar
        r = self.run_day([through, (597, 102.0, 102.1, 101.9, 102.0)])
        self.assertEqual(r["trade"]["reason"], "time")

    def test_a_bar_touching_stop_and_target_counts_as_the_stop(self):
        r = self.run_day([FILL, (597, 101.2, 103.5, 100.0, 101.0)], slip=0.0002)
        t = r["trade"]
        self.assertEqual(t["reason"], "stop")
        self.assertAlmostEqual(t["exit"], 100.29 * (1 - 0.0002))
        self.assertLess(t["r"], -1.0)

    def test_management_is_none_and_the_end_of_day_sale_closes_it(self):
        r = self.run_day([FILL, (597, 101.2, 102.5, 101.2, 102.4)])
        t = r["trade"]
        self.assertEqual((t["reason"], t["exited"], t["exit"]), ("time", 945, 102.4))

    def test_bad_data_days(self):
        self.assertEqual(simulate_day(session(last=900), FvgConfig(), 1e5, 0)["status"], "no_session")
        self.assertEqual(simulate_day(session(drop=set(range(571, 577))), FvgConfig(), 1e5, 0)["status"],
                         "no_opening_range")

    def test_the_mirror_short_is_information_only(self):
        r = simulate_day(session(BEAR, after=[(596, 99.5, 99.95, 99.5, 99.9)]), FvgConfig(), 1e5, 0.0)
        self.assertEqual(r["status"], "bearish_stand_aside")
        self.assertNotIn("trade", r)
        self.assertEqual(r["info_short"]["trade"]["side"], "short")


class BacktestTests(unittest.TestCase):
    def history(self, days, spy_after, qqq_candles=None):
        out = {"SPY": {}, "QQQ": {}}
        for day in weekdays(days):
            out["SPY"][day] = session(after=spy_after)
            out["QQQ"][day] = session(qqq_candles or BEAR, after=[(596, 99.5, 99.95, 99.5, 99.9)])
        return out

    def test_numbers_by_trade_month_and_skip_reason(self):
        result = fvg.backtest(self.history(80, [FILL, TARGET]), slippage_bps=0.0)
        self.assertEqual((result["trades"], result["targets"], result["stops"]), (80, 80, 0))
        self.assertEqual(result["win_rate"], 1.0)
        self.assertAlmostEqual(result["expectancy_r"], 2.0)
        self.assertEqual(result["skipped"], {"bearish_stand_aside": 80})
        self.assertEqual(sum(m["trades"] for m in result["by_month"].values()), 80)
        self.assertEqual(result["short_info"]["trades"], 80)
        self.assertGreater(result["strategy"]["total_return"], 0)

    def test_losing_streak_and_profit_factor(self):
        result = fvg.backtest(self.history(70, [FILL, (597, 101.2, 101.2, 100.0, 100.1)]), slippage_bps=2.0)
        self.assertEqual((result["stops"], result["losing_streak"], result["profit_factor"]), (70, 70, 0.0))

    def test_sensitivity_is_exactly_three_separate_runs(self):
        runs = fvg.sensitivity(self.history(65, [FILL, TARGET]), slippage_bps=0.0)
        self.assertEqual([label[:3] for label, _ in runs], ["(a)", "(b)", "(c)"])


class LiveSignalTests(unittest.TestCase):
    def test_c3_counts_as_closed_once_its_last_minute_has_settled(self):
        bars = session()
        self.assertEqual(live_signal(bars, FvgConfig(), 1e5, 595 * 60 + 10)["status"], "no_pattern")
        s = live_signal(bars, FvgConfig(), 1e5, 595 * 60 + 25)
        self.assertEqual((s["status"], s["qty"], s["entry"], s["stop"], s["target"]), ("place", 247, 101.2, 100.29, 103.02))

    def test_already_played_out_is_never_chased(self):
        s = live_signal(session(after=[FILL]), FvgConfig(), 1e5, 597 * 60 + 5)
        self.assertEqual(s["status"], "already_played_out")

    def test_down_first_gap_and_quiet_mornings(self):
        self.assertEqual(live_signal(session(BEAR), FvgConfig(), 1e5, 600 * 60)["status"], "bearish_stand_aside")
        quiet = session(dict(OPENING))
        self.assertEqual(live_signal(quiet, FvgConfig(), 1e5, 600 * 60)["status"], "no_pattern")
        self.assertEqual(live_signal(quiet, FvgConfig(), 1e5, 661 * 60)["status"], "no_pattern_by_cutoff")
        self.assertEqual(live_signal(quiet, FvgConfig(), 1e5, 580 * 60)["status"], "too_early")


class ExitLabelTests(unittest.TestCase):
    def test_target_stop_and_time_exits_are_labelled(self):
        day = "2026-10-05"
        orders = [{"symbol": "SPY", "side": "buy", "qty": 10, "price": 100.0, "type": "limit", "filled_at": day},
                  {"symbol": "SPY", "side": "sell", "qty": 10, "price": 102.0, "type": "limit", "filled_at": day},
                  {"symbol": "QQQ", "side": "buy", "qty": 5, "price": 200.0, "type": "market", "filled_at": day},
                  {"symbol": "QQQ", "side": "sell", "qty": 5, "price": 199.0, "type": "stop", "filled_at": day}]
        out = {r["symbol"]: r["exit"] for r in summarize_fills(orders, ("SPY", "QQQ"), day)}
        self.assertEqual(out, {"SPY": "target", "QQQ": "stop"})
        orders[1]["type"] = "market"
        self.assertEqual(summarize_fills(orders, ("SPY",), day)[0]["exit"], "time")


class BracketOrderTests(unittest.TestCase):
    def test_the_exact_bracket_body(self):
        session_ = FakeSession(FakeResponse(200, {"id": "o9", "status": "new", "client_order_id": "cid"}))
        api = AlpacaPaper("PKTESTKEY1234", "s3cr3t-value-xyz", session=session_)
        api.submit_bracket_limit("SPY", 247, 101.2, 103.02, 100.29, client_order_id="jvd-20261005-SPY-fvg")
        call = session_.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", PAPER_URL + "/v2/orders"))
        self.assertEqual(call["json"], {
            "symbol": "SPY", "side": "buy", "type": "limit", "time_in_force": "day", "qty": "247",
            "limit_price": "101.20", "order_class": "bracket", "take_profit": {"limit_price": "103.02"},
            "stop_loss": {"stop_price": "100.29"}, "client_order_id": "jvd-20261005-SPY-fvg"})

    def test_bad_brackets_are_refused_before_any_request(self):
        session_ = FakeSession()
        api = AlpacaPaper("PKTESTKEY1234", "s3cr3t-value-xyz", session=session_)
        for args in (("SPY", 0, 101, 103, 100), ("SPY", 1.5, 101, 103, 100), ("SPY", 5, 101, 100, 99),
                     ("SPY", 5, 101, 103, 102), ("SPY", 5, 101, 103, 0), ("bad sym", 5, 101, 103, 100)):
            with self.assertRaises(Exception):
                api.submit_bracket_limit(*args)
        self.assertEqual(session_.calls, [])

    def test_lookup_by_our_own_order_id(self):
        session_ = FakeSession(FakeResponse(200, {"id": "o9", "status": "new", "filled_qty": "0"}))
        api = AlpacaPaper("PKTESTKEY1234", "s3cr3t-value-xyz", session=session_)
        self.assertEqual(api.order_by_client_id("jvd-x")["id"], "o9")
        call = session_.calls[0]
        self.assertEqual((call["method"], call["url"], call["params"]),
                         ("GET", PAPER_URL + "/v2/orders:by_client_order_id", {"client_order_id": "jvd-x"}))


def fvg_broker(after=(), spy=None, **kw):
    quiet = [(m, 200.0, 200.0, 200.0, 200.0) for m in range(570, 960)]
    return DayFakeBroker(bars={"SPY": spy or session(after=after), "QQQ": quiet}, **kw)


class FvgRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        self.logs = []
        self.now = 0.0

    def runner(self, broker, **kw):
        return DayRunner(broker, self.journal, log=self.logs.append, clock=lambda: self.now, rule="fvg", **kw)

    def today(self):
        return self.journal.state()["today"]

    def test_the_default_rule_is_still_the_breakout(self):
        self.assertEqual(DayRunner(DayFakeBroker(), self.journal).rule, "orb")
        with self.assertRaises(ValueError):
            DayRunner(DayFakeBroker(), self.journal, rule="fvg", analyst=lambda day: {})

    def test_the_gap_places_one_bracket_with_the_intent_written_first(self):
        broker = fvg_broker().at(9, 55, 25)
        seen = []
        broker.on_submit = lambda symbol: seen.append(self.journal.state()["today"]["entered"].get(symbol))
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "limit_order_placed")
        self.assertEqual(seen[0]["intent"], True, "the record must say 'buying' before the order goes out")
        self.assertEqual(broker.entry_orders, [{"symbol": "SPY", "qty": 247, "limit": 101.2, "target": 103.02,
                                                "stop": 100.29, "client_order_id": "jvd-20261005-SPY-fvg"}])
        broker.at(9, 56, 0)
        self.assertEqual(runner.step(), "limit_order_waiting")
        self.assertEqual(len(broker.entry_orders), 1)
        self.assertNotIn("SPY", self.today()["decided"])

    def test_a_fill_is_kept_not_swept_and_the_end_of_day_cancels_both_legs_and_sells(self):
        broker = fvg_broker().at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        broker.fill_limit("SPY")
        broker.at(9, 56, 30)
        self.assertNotEqual(runner.step(), "selling_leftovers")
        self.assertEqual(self.today()["decided"]["SPY"], "entered")
        self.assertEqual(self.journal.events("entry")[0]["rule"], "fvg")
        broker.at(15, 45, 5)
        self.now += 60
        self.assertEqual(runner.step(), "flattening")
        self.assertIn("bracket-0001-stop", broker.cancelled)
        self.assertIn("bracket-0001-target", broker.cancelled)
        self.assertEqual(broker.close_calls, ["SPY"])

    def test_a_target_exit_is_recorded_as_a_target(self):
        broker = fvg_broker().at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        broker.fill_limit("SPY")
        broker.hit_target("SPY")
        broker.at(15, 46)
        runner.step()
        results = self.journal.events("trade_result")
        self.assertEqual((results[0]["symbol"], results[0]["exit"]), ("SPY", "target"))

    def test_unfilled_at_eleven_is_cancelled(self):
        broker = fvg_broker().at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        broker.at(11, 0, 5)
        runner.step()
        self.assertEqual(broker.brackets["bracket-0001"]["status"], "canceled")
        self.assertEqual(self.today()["decided"]["SPY"], "never_filled")
        self.assertNotIn("SPY", self.today()["entered"])

    def test_price_under_the_stop_before_a_fill_cancels_it(self):
        crash = [(600, 101.5, 101.5, 100.2, 100.25)]          # (in the fake, the limit simply did not fill)
        broker = fvg_broker(after=crash).at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        broker.at(10, 1, 5)
        runner.step()
        self.assertEqual(broker.brackets["bracket-0001"]["status"], "canceled")
        self.assertEqual(self.today()["decided"]["SPY"], "invalidated")

    def test_a_fill_found_while_cancelling_is_kept_as_a_trade(self):
        broker = fvg_broker().at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        broker.fill_limit("SPY")
        broker.held.pop("SPY")                                # out through a leg already
        broker.at(11, 0, 5)
        runner.step()
        self.assertEqual(self.today()["decided"]["SPY"], "entered")

    def test_down_first_gap_stands_aside_and_played_out_is_not_chased(self):
        broker = fvg_broker(spy=session(BEAR)).at(10, 0, 0)
        self.runner(broker).step()
        self.assertEqual(self.today()["decided"]["SPY"], "bearish_stand_aside")
        self.assertEqual(broker.entry_orders, [])

    def test_already_played_out(self):
        broker = fvg_broker(after=[FILL]).at(9, 57, 5)
        self.runner(broker).step()
        self.assertEqual(self.today()["decided"]["SPY"], "already_played_out")
        self.assertEqual(broker.entry_orders, [])

    def test_a_lost_reply_is_found_by_its_order_id_and_never_sent_twice(self):
        broker = fvg_broker().at(9, 55, 25)
        broker.lose_reply_after_fill = True
        runner = self.runner(broker)
        runner.step()
        broker.lose_reply_after_fill = False
        self.assertTrue(self.today()["entered"]["SPY"]["intent"])
        broker.at(9, 56, 0)
        self.assertEqual(runner.step(), "limit_order_waiting")
        self.assertEqual(self.today()["entered"]["SPY"]["order_id"], "bracket-0001")
        self.assertEqual(len(broker.entry_orders), 1)

    def test_pause_and_stop_cancel_a_resting_buy(self):
        broker = fvg_broker().at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        self.journal.pause()
        broker.at(9, 56)
        self.assertEqual(runner.step(), "paused")
        self.assertEqual(broker.brackets["bracket-0001"]["status"], "canceled")

        other = Journal(Path(self._tmp.name) / "b")
        broker = fvg_broker().at(9, 55, 25)
        runner = DayRunner(broker, other, log=self.logs.append, clock=lambda: self.now, rule="fvg")
        runner.step()
        self.assertEqual(runner.close_out(), "flat")
        self.assertEqual(broker.brackets["bracket-0001"]["status"], "canceled")

    def test_the_loss_halt_cancels_a_resting_buy(self):
        broker = fvg_broker(last_equity=102_000.0).at(9, 55, 25)
        runner = self.runner(broker)
        runner.step()
        self.assertEqual(broker.entry_orders, [], "already down 1.9%: halted before any buy")


class PluginRuleTests(unittest.TestCase):
    def test_jarvis_says_which_rule_is_active(self):
        from plugins import day_trading
        self.assertIn("opening-range breakout", day_trading._rule_sentence({}))
        self.assertIn("fair-value-gap", day_trading._rule_sentence({"rule": "fvg"}))


if __name__ == "__main__":
    unittest.main()
