import unittest

from trading.risk import Context, Limits, Order, plan_orders, vet

EQUITY = 100_000.0


def ctx(**kw):
    base = dict(equity=EQUITY, last_equity=EQUITY, cash=EQUITY, positions={})
    base.update(kw)
    return Context(**base)


def held(symbol_values: dict) -> dict:
    return {s: {"qty": v / 100, "qty_text": f"{v / 100:.4f}", "market_value": v}
            for s, v in symbol_values.items()}


class PlanTests(unittest.TestCase):
    def test_empty_account_buys_each_target_share(self):
        sells, buys = plan_orders({"SPY": 1 / 3, "QQQ": 1 / 3, "GLD": 1 / 3}, ctx())
        self.assertEqual(sells, [])
        self.assertEqual(sorted(o.symbol for o in buys), ["GLD", "QQQ", "SPY"])
        self.assertAlmostEqual(buys[0].notional, EQUITY * 0.98 / 3, delta=0.01)

    def test_a_full_plan_always_fits_inside_the_no_margin_rule(self):
        context = ctx()
        _, buys = plan_orders({"SPY": 1 / 3, "QQQ": 1 / 3, "GLD": 1 / 3}, context)
        allowed, rejected = vet(buys, context)
        self.assertEqual((len(allowed), rejected), (3, []))
        self.assertLessEqual(sum(o.dollars for o in allowed), EQUITY * 0.98)

    def test_dropped_fund_is_sold_in_full_using_the_exact_share_count(self):
        sells, buys = plan_orders({}, ctx(positions=held({"TLT": 30_000.0}), cash=70_000.0))
        self.assertEqual((sells[0].symbol, sells[0].side, sells[0].qty), ("TLT", "sell", "300.0000"))
        self.assertIsNone(sells[0].notional)
        self.assertEqual(buys, [])

    def test_small_drift_is_left_alone_and_big_drift_is_trimmed(self):
        positions = held({"SPY": 34_000.0, "QQQ": 40_000.0})
        sells, buys = plan_orders({"SPY": 1 / 3, "QQQ": 1 / 3},
                                  ctx(positions=positions, cash=26_000.0))
        self.assertEqual([o.symbol for o in sells], ["QQQ"])
        self.assertEqual(sells[0].why, "trim")
        self.assertEqual(buys, [])


class VetTests(unittest.TestCase):
    def labels(self, orders, context, limits=Limits()):
        allowed, rejected = vet(orders, context, limits)
        return [o.symbol for o in allowed], [reason for _, reason in rejected]

    def test_ordinary_buy_passes(self):
        ok, bad = self.labels([Order("SPY", "buy", notional=33_000.0)], ctx())
        self.assertEqual((ok, bad), (["SPY"], []))

    def test_unlisted_symbol_is_refused(self):
        self.assertEqual(self.labels([Order("TSLA", "buy", notional=1_000.0)], ctx())[1], ["not_allowed"])

    def test_cannot_sell_what_is_not_held(self):
        self.assertEqual(self.labels([Order("SPY", "sell", qty="5")], ctx())[1], ["would_short"])

    def test_cannot_trim_more_than_is_held(self):
        context = ctx(positions=held({"SPY": 1_000.0}))
        self.assertEqual(self.labels([Order("SPY", "sell", notional=5_000.0)], context)[1], ["would_short"])

    def test_oversized_order_and_position_are_refused(self):
        self.assertEqual(self.labels([Order("SPY", "buy", notional=45_000.0)], ctx())[1], ["order_too_large"])
        context = ctx(positions=held({"SPY": 30_000.0}), cash=70_000.0)
        self.assertEqual(self.labels([Order("SPY", "buy", notional=15_000.0)], context)[1],
                         ["position_too_large"])

    def test_dollar_cap_applies_when_set(self):
        limits = Limits(max_order_usd=5_000.0)
        self.assertEqual(self.labels([Order("SPY", "buy", notional=10_000.0)], ctx(), limits)[1],
                         ["order_over_dollar_cap"])

    def test_no_margin_buys_stop_when_cash_runs_out(self):
        context = ctx(cash=50_000.0, positions=held({"GLD": 50_000.0}))
        orders = [Order("SPY", "buy", notional=33_000.0), Order("QQQ", "buy", notional=33_000.0)]
        ok, bad = self.labels(orders, context)
        self.assertEqual((ok, bad), (["SPY"], ["not_enough_cash"]))

    def test_cash_buffer_is_kept(self):
        context = ctx(cash=33_000.0, positions=held({"GLD": 67_000.0}))
        self.assertEqual(self.labels([Order("SPY", "buy", notional=33_000.0)], context)[1],
                         ["not_enough_cash"])

    def test_daily_loss_halts_buying_but_not_selling(self):
        context = ctx(equity=96_000.0, last_equity=100_000.0, cash=96_000.0)
        self.assertEqual(self.labels([Order("SPY", "buy", notional=10_000.0)], context)[1], ["daily_loss_halt"])
        sell_context = ctx(equity=96_000.0, last_equity=100_000.0, cash=66_000.0,
                           positions=held({"TLT": 30_000.0}))
        self.assertEqual(self.labels([Order("TLT", "sell", qty="300")], sell_context)[0], ["TLT"])

    def test_order_count_is_capped_per_day(self):
        context = ctx(orders_today=12)
        self.assertEqual(self.labels([Order("SPY", "buy", notional=1_000.0)], context)[1],
                         ["too_many_orders_today"])

    def test_same_day_round_trip_is_refused(self):
        context = ctx(positions=held({"SPY": 10_000.0}), bought_today=frozenset({"SPY"}))
        self.assertEqual(self.labels([Order("SPY", "sell", qty="100")], context)[1], ["bought_today"])

    def test_blocked_account_trades_nothing(self):
        self.assertEqual(self.labels([Order("SPY", "buy", notional=1_000.0)], ctx(blocked=True))[1],
                         ["account_blocked"])


if __name__ == "__main__":
    unittest.main()
