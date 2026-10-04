import tempfile
import unittest
from pathlib import Path

from trading.broker import BrokerError
from trading.journal import Journal
from trading.risk import Order
from trading.runner import Runner, parse_ts
from trading.strategy import UNIVERSE

from trading_fakes import FALLING, RISING, FakeBroker, make_bars

TUESDAY = "2026-10-06"
FRIDAY = "2026-10-09"
NEXT_TUESDAY = "2026-10-13"


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.journal = Journal(Path(self._tmp.name))
        self.logs = []

    def runner(self, broker, **kw):
        return Runner(broker, self.journal, log=self.logs.append, sleep=lambda _s: None,
                      fill_wait=0.0, **kw)

    def kinds(self, kind):
        return self.journal.events(kind)


class TimestampTests(unittest.TestCase):
    def test_parses_alpaca_styles(self):
        self.assertEqual(parse_ts("2026-10-06T10:30:00-04:00").utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(parse_ts("2026-10-06T14:30:00Z").hour, 14)
        self.assertEqual(parse_ts("2026-10-06T10:30:00.123456789-04:00").microsecond, 123456)


class DailyDecisionTests(RunnerCase):
    def test_closed_market_does_nothing(self):
        broker = FakeBroker(is_open=False)
        self.assertEqual(self.runner(broker).step(), "market_closed")
        self.assertEqual(broker.orders, [])

    def test_first_session_buys_the_three_strongest_with_equal_shares(self):
        broker = FakeBroker(day=TUESDAY)
        self.assertEqual(self.runner(broker).step(), "rebalanced")
        self.assertEqual(sorted(o["symbol"] for o in broker.orders), ["GLD", "QQQ", "SPY"])
        for order in broker.orders:
            self.assertEqual(order["side"], "buy")
            self.assertAlmostEqual(order["notional"], 100_000 * 0.98 / 3, delta=0.01)
        state = self.journal.state()
        self.assertEqual((state["start_equity"], state["last_rebalance_date"]), (100_000.0, TUESDAY))
        self.assertGreater(broker.cash, 0)

    def test_running_twice_in_a_day_sends_nothing_new(self):
        broker = FakeBroker(day=TUESDAY)
        runner = self.runner(broker)
        runner.step()
        sent = len(broker.orders)
        self.assertEqual(runner.step(), "already_ran")
        self.assertEqual(len(broker.orders), sent)

    def test_later_days_in_the_same_week_are_not_decision_days(self):
        broker = FakeBroker(day=TUESDAY)
        self.runner(broker).step()
        broker.day = FRIDAY
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "not_due")
        self.assertEqual(len(broker.orders), 3)

    def test_next_week_rebalances_and_a_matching_account_needs_no_trades(self):
        broker = FakeBroker(day=TUESDAY)
        self.runner(broker).step()
        broker.day, broker.bars = NEXT_TUESDAY, make_bars(NEXT_TUESDAY, drifts=RISING)
        self.assertEqual(self.runner(broker).step(), "rebalanced")
        self.assertEqual(len(broker.orders), 3)

    def test_decisions_only_happen_inside_the_window(self):
        for minutes_left in (10, 380):
            broker = FakeBroker(day=TUESDAY, minutes_left=minutes_left)
            self.assertEqual(self.runner(broker).step(), "outside_window")
            self.assertEqual(broker.orders, [])

    def test_pause_blocks_orders_but_still_records_the_account(self):
        broker = FakeBroker(day=TUESDAY)
        self.journal.pause()
        self.assertEqual(self.runner(broker).step(), "paused")
        self.assertEqual(broker.orders, [])
        self.assertEqual(self.journal.state()["last_seen"]["equity"], 100_000.0)
        self.journal.resume()
        self.assertEqual(self.runner(broker).step(), "rebalanced")

    def test_a_blocked_account_is_left_alone(self):
        broker = FakeBroker(day=TUESDAY, blocked=True)
        self.assertEqual(self.runner(broker).step(), "account_blocked")
        self.assertEqual(broker.orders, [])

    def test_too_little_history_is_reported_not_guessed(self):
        broker = FakeBroker(day=TUESDAY, bars=make_bars(TUESDAY, count=40))
        self.assertEqual(self.runner(broker).step(), "not_enough_history")
        self.assertEqual(broker.orders, [])

    def test_today_s_unfinished_candle_is_never_used(self):
        bars = make_bars(TUESDAY, drifts=RISING)
        bars["SPY"].append((TUESDAY, 1.0))                 # a crashed partial candle for today
        broker = FakeBroker(day=TUESDAY, bars=bars)
        self.runner(broker).step()
        self.assertIn("SPY", [o["symbol"] for o in broker.orders])


class SellsBeforeBuysTests(RunnerCase):
    def test_exits_fund_the_buys_and_no_margin_is_used(self):
        broker = FakeBroker(day=TUESDAY, positions={"TLT": 100_000.0}, equity=100_000.0)
        self.assertEqual(self.runner(broker).step(), "rebalanced")
        sides = [o["side"] for o in broker.orders]
        self.assertEqual(sides[0], "sell")
        self.assertEqual(broker.orders[0]["qty"] is not None, True)
        self.assertTrue(all(s == "buy" for s in sides[1:]))
        self.assertGreaterEqual(broker.cash, 0.0)
        self.assertEqual(sorted(broker.held), ["GLD", "QQQ", "SPY"])

    def test_falling_markets_move_to_cash(self):
        broker = FakeBroker(day=TUESDAY, positions={"SPY": 50_000.0, "QQQ": 50_000.0},
                            bars=make_bars(TUESDAY, drifts=FALLING))
        self.assertEqual(self.runner(broker).step(), "rebalanced")
        self.assertEqual(broker.held, {})
        self.assertEqual(broker.cash, 100_000.0)


class SafetyTests(RunnerCase):
    def test_a_bad_day_stops_new_buying_and_says_why(self):
        broker = FakeBroker(day=TUESDAY, last_equity=110_000.0)           # down ~9% on the day
        self.runner(broker).step()
        self.assertEqual(broker.orders, [])
        self.assertIn("daily_loss_halt", [e["reason"] for e in self.kinds("rejected")])

    def test_resending_the_same_order_the_same_day_is_refused_by_its_id(self):
        broker = FakeBroker(day=TUESDAY)
        runner = self.runner(broker)
        order = Order("SPY", "buy", notional=1_000.0)
        self.assertEqual(runner._send([order], TUESDAY), 0)
        self.assertEqual(runner._send([order], TUESDAY), 1)
        self.assertEqual(len(broker.orders), 1)

    def test_a_failed_order_is_retried_then_given_up_on_for_the_day(self):
        broker = FakeBroker(day=TUESDAY)
        broker.fail_next_orders = 99
        runner = self.runner(broker)
        self.assertEqual(runner.step(), "rebalance_incomplete")
        self.assertEqual(runner.step(), "rebalance_incomplete")
        self.assertEqual(runner.step(), "rebalance_incomplete")
        self.assertEqual(runner.step(), "already_ran")
        self.assertIsNone(self.journal.state().get("last_rebalance_date"))

    def test_journal_holds_labels_and_numbers_not_secrets(self):
        broker = FakeBroker(day=TUESDAY)
        self.runner(broker).step()
        text = self.journal.events_path.read_text()
        for needle in ("PK", "secret", "APCA"):
            self.assertNotIn(needle, text)

    def test_only_listed_funds_are_ever_ordered(self):
        broker = FakeBroker(day=TUESDAY)
        self.runner(broker).step()
        self.assertTrue(all(o["symbol"] in UNIVERSE for o in broker.orders))


class LoopTests(RunnerCase):
    def test_an_error_in_one_pass_never_ends_the_loop(self):
        class Boom(FakeBroker):
            calls = 0
            def clock(self):
                Boom.calls += 1
                if Boom.calls == 1:
                    raise BrokerError("Could not reach Alpaca. Check the internet connection.")
                if Boom.calls == 2:
                    raise ValueError("unexpected")
                return super().clock()

        class Stop:
            def __init__(self): self.n, self.delays = 0, []
            def is_set(self): return self.n >= 4
            def wait(self, delay): self.n += 1; self.delays.append(delay)

        stop = Stop()
        self.runner(Boom(day=TUESDAY)).run_forever(stop)
        self.assertEqual(stop.n, 4)
        errors = [e["label"] for e in self.kinds("error")]
        self.assertEqual(errors, ["error:broker", "error:ValueError"])
        self.assertGreater(stop.delays[1], stop.delays[0])          # backs off while failing
        self.assertLessEqual(max(stop.delays), 900.0)


class PlanTests(RunnerCase):
    def test_plan_describes_but_sends_nothing(self):
        broker = FakeBroker(day=TUESDAY, is_open=False)
        plan, clock = self.runner(broker).plan_now()
        self.assertFalse(clock["is_open"])
        self.assertEqual(sorted(plan.targets), ["GLD", "QQQ", "SPY"])
        self.assertEqual(len(plan.buys), 3)
        self.assertEqual(broker.orders, [])


if __name__ == "__main__":
    unittest.main()
