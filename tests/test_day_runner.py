import tempfile
import threading
import unittest
from pathlib import Path

from trading.broker import BrokerError
from trading.day.rule import DayConfig
from trading.day.runner import DayRunner, review_latest_session
from trading.journal import Journal

from day_fakes import DAY, DayFakeBroker, day_bars


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        self.logs = []
        self.now = 0.0

    def runner(self, broker, **kw):
        return DayRunner(broker, self.journal, log=self.logs.append, clock=lambda: self.now, **kw)

    def advance(self, seconds=60.0):
        self.now += seconds


class EntryTests(RunnerCase):
    def test_before_the_opening_range_is_done_it_just_waits(self):
        broker = DayFakeBroker().at(9, 40)
        self.assertEqual(self.runner(broker).step(), "building_opening_range")
        self.assertEqual(broker.entry_orders, [])

    def test_a_fresh_breakout_is_bought_once_with_a_stop_and_a_stable_order_id(self):
        broker = DayFakeBroker()                                   # 9:51:05, SPY broke out at 9:50
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "bought")
        spy = [o for o in broker.entry_orders if o["symbol"] == "SPY"]
        self.assertEqual(len(spy), 1)
        self.assertEqual((spy[0]["qty"], spy[0]["stop"]), (247, 100.0))
        self.assertEqual(spy[0]["client_order_id"], "jvd-20261005-SPY-in")
        self.assertEqual(self.journal.events("entry")[0]["symbol"], "SPY")
        count = len(broker.entry_orders)
        broker.at(9, 51, 25)
        runner.step()
        self.assertEqual(len(broker.entry_orders), count, "a second pass must not buy again")

    def test_a_restart_cannot_buy_the_same_fund_twice(self):
        broker = DayFakeBroker()
        self.runner(broker).step()
        count = len(broker.entry_orders)
        broker.at(9, 51, 40)
        self.runner(broker).step()                                 # a brand new runner, same record
        self.assertEqual(len(broker.entry_orders), count)

    def test_even_with_the_record_wiped_the_order_id_blocks_a_duplicate(self):
        broker = DayFakeBroker()
        self.runner(broker).step()
        self.journal.update_state(today=None)                     # lose the day's notes
        broker.at(9, 51, 40)
        self.runner(broker).step()                                 # the leftover sweep fires first
        self.assertEqual(len(broker.entry_orders), 2)              # SPY and QQQ, once each
        self.assertEqual(sorted(broker.close_calls), ["QQQ", "SPY"])
        self.assertEqual((broker.held, broker.stops), ({}, {}))

    def test_an_old_breakout_is_recorded_as_skipped_not_chased(self):
        broker = DayFakeBroker().at(10, 30)
        runner = self.runner(broker)
        runner.step()
        self.assertEqual(broker.entry_orders, [])
        statuses = {e["symbol"]: e["status"] for e in self.journal.events("decision")}
        self.assertEqual(statuses, {"SPY": "breakout_stale", "QQQ": "breakout_stale"})

    def test_a_quiet_day_buys_nothing_and_gives_up_at_two(self):
        quiet = {"SPY": day_bars(breakout=None), "QQQ": day_bars(breakout=None, rng=(200.0, 201.2))}
        broker = DayFakeBroker(bars=quiet).at(11, 0)
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "watching")
        broker.at(14, 0, 10)
        runner.step()
        self.assertEqual(broker.entry_orders, [])
        self.assertEqual({e["status"] for e in self.journal.events("decision")}, {"late"})
        self.assertEqual(runner.step(), "done_for_entries")

    def test_pause_stops_buying(self):
        self.journal.pause()
        broker = DayFakeBroker()
        self.assertEqual(self.runner(broker).step(), "paused")
        self.assertEqual(broker.entry_orders, [])

    def test_price_already_through_the_stop_or_far_above_the_signal_is_not_bought(self):
        broker = DayFakeBroker()
        broker.price_override = {"SPY": 99.95, "QQQ": 300.0}
        self.runner(broker).step()
        self.assertEqual(broker.entry_orders, [])
        statuses = {e["symbol"]: e["status"] for e in self.journal.events("decision")}
        self.assertEqual(statuses, {"SPY": "price_through_stop", "QQQ": "price_ran_away"})

    def test_it_never_spends_more_cash_than_it_has(self):
        broker = DayFakeBroker(equity=100_000.0)
        broker.cash = 5_000.0
        self.runner(broker).step()
        for order in broker.entry_orders:
            self.assertLessEqual(order["qty"] * broker._price(order["symbol"]), 5_000.0)
        self.assertGreaterEqual(broker.cash, 0.0)

    def test_failed_orders_retry_then_give_up_after_three(self):
        broker = DayFakeBroker()
        broker.fail_entries = 99
        runner = self.runner(broker)
        for second in (5, 15, 25, 35):
            broker.at(9, 51, second)
            runner.step()
        self.assertEqual(len(self.journal.events("order_error")), 6)    # 3 tries x 2 funds
        self.assertEqual({e["status"] for e in self.journal.events("decision")}, {"order_failed"})
        self.assertEqual(broker.entry_orders, [])

    def test_a_lost_reply_is_recognised_when_alpaca_says_the_id_exists(self):
        broker = DayFakeBroker()
        broker.client_ids.add("jvd-20261005-SPY-in")
        broker.entry_orders.append({"symbol": "SPY", "qty": 1, "stop": 1, "client_order_id": "x"})
        self.runner(broker).step()
        entries = {e["symbol"] for e in self.journal.events("entry")}
        self.assertIn("SPY", entries)


class SafetyTests(RunnerCase):
    def test_everything_is_sold_fifteen_minutes_before_the_close_and_the_day_recorded(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()                                              # bought both at 9:51
        broker.at(15, 44, 59)
        self.advance(1000)
        self.assertNotEqual(runner.step(), "flattening")
        broker.at(15, 45, 0)
        self.assertEqual(runner.step(), "flattening")
        self.assertEqual(sorted(broker.close_calls), ["QQQ", "SPY"])
        self.assertEqual(broker.stops, {}, "each fund's stop is cancelled before it is sold")
        self.advance(60)
        self.assertEqual(runner.step(), "flat_for_the_day")
        results = self.journal.events("trade_result")
        self.assertEqual({r["symbol"] for r in results}, {"SPY", "QQQ"})
        self.assertTrue(all(r["pnl"] > 0 and r["exit"] == "time" for r in results))
        snapshot = self.journal.events("snapshot")[-1]
        self.assertEqual(snapshot["date"], DAY)
        self.assertAlmostEqual(snapshot["equity"], broker.equity)
        self.advance(60)
        runner.step()
        self.assertEqual(len(self.journal.events("snapshot")), 1, "the day is recorded once")

    def test_pause_never_strands_a_position(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        self.journal.pause()
        broker.at(15, 50)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")
        self.assertEqual(broker.held, {})

    def test_a_stopped_out_trade_is_recorded_as_a_stop(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        broker.hit_stop("SPY", 100.0)
        broker.at(15, 46)
        self.advance(1000)
        runner.step()
        self.advance(60)
        runner.step()
        by_symbol = {r["symbol"]: r for r in self.journal.events("trade_result")}
        self.assertEqual(by_symbol["SPY"]["exit"], "stop")
        self.assertLess(by_symbol["SPY"]["pnl"], 0)

    def test_shares_left_from_an_earlier_day_are_sold_first_thing(self):
        broker = DayFakeBroker(held={"SPY": 50}).at(9, 31)
        self.assertEqual(self.runner(broker).step(), "selling_leftovers")
        self.assertEqual(broker.close_calls, ["SPY"])
        self.assertEqual(self.journal.events("sweep")[0]["symbols"], ["SPY"])

    def test_it_refuses_to_touch_an_account_that_holds_anything_else(self):
        broker = DayFakeBroker(held={"GLD": 100, "SPY": 10})
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "not_a_dedicated_account")
        self.assertEqual(broker.close_calls, [])
        self.assertEqual(broker.entry_orders, [])
        broker.at(15, 50)
        self.advance(1000)
        self.assertEqual(runner.step(), "not_a_dedicated_account")
        self.assertEqual(broker.close_calls, [], "shares it did not buy itself are never sold")
        self.assertEqual(len(self.journal.events("error")), 1, "the problem is noted once, not every pass")

    def test_a_one_percent_loss_sells_everything_and_ends_buying_for_the_day(self):
        broker = DayFakeBroker(last_equity=102_000.0, equity=100_000.0)
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "daily_loss_halt")
        self.assertEqual(broker.entry_orders, [])
        self.assertEqual(self.journal.events("halt")[0]["day"], DAY)
        broker.at(9, 55)
        self.advance(1000)
        self.assertEqual(runner.step(), "daily_loss_halt")
        self.assertEqual(broker.entry_orders, [])

    def test_the_loss_halt_sells_what_it_holds(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()                                              # long both
        broker.last_equity = broker.equity * 1.02                  # now ~2% down on the day
        broker.at(11, 0)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")
        self.assertEqual(broker.held, {})

    def test_an_early_close_day_is_sat_out_and_still_flattened_early(self):
        broker = DayFakeBroker(close_minute=780).at(10, 5)
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "early_close_sits_out")
        self.assertEqual(broker.entry_orders, [])
        broker.held["SPY"] = 5
        self.journal.update_state(today={"day": DAY, "decided": {}, "entered": {"SPY": {}},
                                         "attempts": {}, "halted": False, "finished": False})
        broker.at(12, 46)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")

    def test_a_blocked_account_trades_nothing(self):
        broker = DayFakeBroker(blocked=True)
        self.assertEqual(self.runner(broker).step(), "account_blocked")
        self.assertEqual(broker.entry_orders, [])

    def test_when_the_market_is_closed_it_idles_with_a_sensible_wait(self):
        broker = DayFakeBroker(is_open=False).at(20, 0)
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "market_closed")
        self.assertTrue(300.0 <= runner.hint <= 600.0)
        self.assertEqual(broker.close_calls, [])

    def test_selling_is_retried_but_not_hammered(self):
        broker = DayFakeBroker(held={"SPY": 5}).at(9, 31)
        calls = []
        broker.close_position = lambda symbol: calls.append(symbol)    # accepted but not filled yet
        runner = self.runner(broker)
        runner.step()
        runner.step()
        self.assertEqual(len(calls), 1, "a second request inside 10 seconds is held back")
        self.advance(11)
        runner.step()
        self.assertEqual(len(calls), 2)


class StopAndLoopTests(RunnerCase):
    def test_stopping_sells_what_is_held_while_the_market_is_open(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        self.assertEqual(runner.close_out(), "sold")
        self.assertEqual(broker.held, {})

    def test_stopping_after_the_close_cannot_sell_and_says_so(self):
        broker = DayFakeBroker(held={"SPY": 5}, is_open=False)
        self.assertEqual(self.runner(broker).close_out(), "held")
        self.assertEqual(DayFakeBroker(is_open=False).positions(), {})
        self.assertEqual(self.runner(DayFakeBroker()).close_out(), "flat")

    def test_the_loop_survives_errors_and_stops_on_request(self):
        class Flaky(DayFakeBroker):
            calls = 0

            def clock(self):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise BrokerError("Alpaca is having trouble right now.")
                if Flaky.calls == 2:
                    raise ZeroDivisionError
                return super().clock()

        stop = threading.Event()
        runner = self.runner(Flaky())
        original = stop.wait
        waits = []

        def wait(delay):
            waits.append(delay)
            if len(waits) >= 4:
                stop.set()
            return original(0)
        stop.wait = wait
        runner.run_forever(stop)
        self.assertGreaterEqual(len(waits), 4)
        labels = [e["label"] for e in self.journal.events("error")]
        self.assertEqual(labels, ["error:broker", "error:ZeroDivisionError"])
        self.assertGreater(waits[1], waits[0], "waits grow while it keeps failing")

    def test_the_start_line_is_recorded_once(self):
        broker = DayFakeBroker().at(9, 40)
        runner = self.runner(broker)
        runner.step()
        runner.step()
        self.assertEqual(len(self.journal.events("start")), 1)
        self.assertEqual(self.journal.state()["start_equity"], 100_000.0)


class ReviewFindingTests(RunnerCase):
    """Each of these failed against the first version of the runner."""

    def test_stopping_never_sells_shares_it_did_not_buy_itself(self):
        broker = DayFakeBroker(held={"IWM": 100, "GLD": 50})
        self.assertEqual(self.runner(broker).close_out(), "flat")
        self.assertEqual(broker.held, {"IWM": 100, "GLD": 50})
        self.assertEqual(broker.close_calls, [])

    def test_stopping_sells_its_own_buys_but_leaves_the_rest(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        broker.held["GLD"] = 50
        self.assertEqual(runner.close_out(), "sold")
        self.assertEqual(broker.held, {"GLD": 50})

    def test_other_holdings_do_not_stop_it_selling_its_own_position_at_the_close(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()                                              # bought SPY and QQQ
        broker.held["AAPL"] = 10                                   # someone buys something by hand
        broker.at(15, 46)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")
        self.assertEqual(broker.held, {"AAPL": 10})
        self.advance(60)
        self.assertEqual(runner.step(), "not_a_dedicated_account")
        self.assertEqual(len(self.journal.events("snapshot")), 1)

    def test_a_sale_is_retried_through_a_slow_stop_cancellation(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        real_cancel = broker.cancel_order
        broker.cancel_order = lambda order_id: None                # the cancel has not landed yet
        broker.at(15, 46)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")
        self.assertTrue(broker.held, "the sale was refused while the stop still held the shares")
        self.assertTrue(self.journal.events("error"))
        broker.cancel_order = real_cancel
        self.advance(11)
        runner.step()
        self.assertEqual(broker.held, {})

    def test_an_outage_before_the_close_is_retried_quickly_not_after_the_bell(self):
        class Down(DayFakeBroker):
            down = False

            def account(self):
                if Down.down:
                    raise BrokerError("Could not reach Alpaca. Check the internet connection.")
                return super().account()

        broker = Down()
        runner = self.runner(broker)
        runner.step()                                              # in session, holding
        Down.down = True
        waits = []
        stop = threading.Event()

        def wait(delay):
            waits.append(delay)
            if len(waits) >= 8:
                stop.set()
            return False
        stop.wait = wait
        runner.run_forever(stop)
        self.assertEqual(len(waits), 8)
        self.assertLessEqual(max(waits), 30.0)

    def test_an_unreadable_close_time_still_sells_on_time(self):
        class NoClose(DayFakeBroker):
            def clock(self):
                return {**super().clock(), "next_close": ""}
        broker = NoClose()
        runner = self.runner(broker)
        runner.step()
        broker.at(15, 46)
        self.advance(1000)
        self.assertEqual(runner.step(), "flattening")
        self.assertEqual(broker.held, {})

    def test_a_buy_whose_reply_was_lost_is_not_swept_away_or_repeated(self):
        broker = DayFakeBroker()
        broker.lose_reply_after_fill = True
        runner = self.runner(broker)
        runner.step()                                              # filled, but the reply never arrived
        self.assertTrue(broker.held)
        broker.at(9, 51, 25)
        self.advance(15)
        runner.step()
        self.assertEqual(broker.close_calls, [], "its own fresh position must not be sold as a leftover")
        self.assertEqual(len(broker.entry_orders), 2, "and it must not be bought again")
        self.assertEqual(self.journal.state()["today"]["decided"], {"SPY": "entered", "QQQ": "entered"})

    def test_a_clean_refusal_leaves_nothing_marked_as_bought(self):
        broker = DayFakeBroker()
        broker.fail_entries = 2
        broker.entry_error = "Alpaca refused the request (403). insufficient buying power"
        self.runner(broker).step()
        self.assertEqual(self.journal.state()["today"]["entered"], {})

    def test_a_different_account_than_before_is_not_touched(self):
        broker = DayFakeBroker()
        runner = self.runner(broker)
        runner.step()
        self.assertEqual(self.journal.state()["account_id"], "acct-day")
        broker.account_id = "someone-elses"
        broker.at(15, 50)
        self.advance(1000)
        self.assertEqual(runner.step(), "account_changed")
        self.assertEqual(broker.close_calls, [])
        self.assertTrue(broker.held)

    def test_under_twenty_five_thousand_it_does_not_day_trade(self):
        broker = DayFakeBroker(equity=20_000.0, last_equity=20_000.0)
        self.assertEqual(self.runner(broker).step(), "below_day_trading_minimum")
        self.assertEqual(broker.entry_orders, [])

    def test_a_small_account_still_gets_its_leftovers_sold(self):
        broker = DayFakeBroker(equity=20_000.0, last_equity=20_000.0, held={"SPY": 5}).at(9, 31)
        self.assertEqual(self.runner(broker).step(), "selling_leftovers")
        self.assertEqual(broker.close_calls, ["SPY"])


class ReviewTests(unittest.TestCase):
    def test_review_replays_the_latest_full_session_without_sending_anything(self):
        class Week(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                from day_fakes import raw_bars
                return {s: raw_bars(DAY, self.bars[s]) for s in symbols}
        broker = Week()
        review = review_latest_session(broker, DayConfig())
        self.assertEqual(review["day"], DAY)
        self.assertEqual(review["results"]["SPY"]["status"], "traded")
        self.assertEqual(broker.entry_orders, [])

    def test_review_with_no_prices_says_so(self):
        class Empty(DayFakeBroker):
            def minute_bars(self, symbols, start, end=None, feed="iex", **_):
                return {s: [] for s in symbols}
        self.assertIsNone(review_latest_session(Empty(), DayConfig())["day"])


if __name__ == "__main__":
    unittest.main()
