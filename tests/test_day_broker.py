import unittest

from trading.broker import DATA_URL, PAPER_URL, AlpacaPaper, BrokerError

from trading_fakes import FakeResponse, FakeSession

KEY, SECRET = "PKTESTKEY1234", "s3cr3t-value-xyz"


def client(*responses):
    session = FakeSession(*responses)
    return AlpacaPaper(KEY, SECRET, session=session), session


def bar(t, o=100.0, h=101.0, l=99.0, c=100.5):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 10}


class MinuteBarTests(unittest.TestCase):
    def test_pages_are_followed_and_the_request_is_for_raw_one_minute_candles(self):
        api, session = client(
            FakeResponse(200, {"bars": {"SPY": [bar("2026-10-05T13:30:00Z")]}, "next_page_token": "abc"}),
            FakeResponse(200, {"bars": {"SPY": [bar("2026-10-05T13:31:00Z")], "QQQ": [bar("2026-10-05T13:30:00Z")]}}))
        out = api.minute_bars(["SPY", "QQQ"], "2026-10-05T00:00:00-04:00")
        self.assertEqual([b[0] for b in out["SPY"]], ["2026-10-05T13:30:00Z", "2026-10-05T13:31:00Z"])
        self.assertEqual(out["QQQ"][0], ("2026-10-05T13:30:00Z", 100.0, 101.0, 99.0, 100.5))
        first, second = session.calls
        self.assertTrue(first["url"].startswith(DATA_URL))
        self.assertEqual((first["params"]["timeframe"], first["params"]["feed"]), ("1Min", "iex"))
        self.assertNotIn("adjustment", first["params"])
        self.assertEqual(second["params"]["page_token"], "abc")

    def test_zero_or_missing_prices_are_dropped(self):
        api, _ = client(FakeResponse(200, {"bars": {"SPY": [bar("2026-10-05T13:30:00Z", o=0),
                                                            bar("2026-10-05T13:31:00Z"),
                                                            {"o": 1, "h": 1, "l": 1, "c": 1}]}}))
        self.assertEqual(len(api.minute_bars(["SPY"], "2026-10-05")["SPY"]), 1)

    def test_the_feed_is_validated_and_end_is_passed_through(self):
        api, session = client(FakeResponse(200, {"bars": {}}))
        with self.assertRaises(BrokerError):
            api.minute_bars(["SPY"], "2026-10-05", feed="nasdaq")
        api.minute_bars(["SPY"], "2026-10-05", "2026-10-06", feed="sip")
        self.assertEqual((session.calls[0]["params"]["feed"], session.calls[0]["params"]["end"]),
                         ("sip", "2026-10-06"))

    def test_an_endless_history_stops_with_a_plain_message(self):
        looping = [FakeResponse(200, {"bars": {"SPY": [bar("2026-10-05T13:30:00Z")]}, "next_page_token": "t"})
                   for _ in range(3)]
        api, _ = client(*looping)
        with self.assertRaises(BrokerError) as caught:
            api.minute_bars(["SPY"], "2020-01-01", max_pages=3)
        self.assertIn("fewer years", str(caught.exception))

    def test_progress_is_reported_per_page(self):
        api, _ = client(FakeResponse(200, {"bars": {}, "next_page_token": "a"}), FakeResponse(200, {"bars": {}}))
        seen = []
        api.minute_bars(["SPY"], "2026-10-05", progress=seen.append)
        self.assertEqual(seen, [1, 2])


class EntryOrderTests(unittest.TestCase):
    def test_the_buy_carries_a_stop_that_lives_at_alpaca(self):
        api, session = client(FakeResponse(200, {"id": "o1", "status": "accepted", "client_order_id": "cid"}))
        result = api.submit_entry_with_stop("SPY", 247, 100.0, client_order_id="cid")
        call = session.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", PAPER_URL + "/v2/orders"))
        self.assertEqual(call["json"], {"symbol": "SPY", "side": "buy", "type": "market",
                                        "time_in_force": "day", "qty": "247", "order_class": "oto",
                                        "stop_loss": {"stop_price": "100.00"}, "client_order_id": "cid"})
        self.assertEqual(result["id"], "o1")

    def test_bad_entries_are_refused_before_any_request(self):
        api, session = client()
        for args in (("SPY", 0, 100.0), ("SPY", 1.5, 100.0), ("SPY", -3, 100.0), ("SPY", 5, 0),
                     ("SPY", 5, -1.0), ("not a symbol", 5, 100.0), ("", 5, 100.0)):
            with self.assertRaises(BrokerError):
                api.submit_entry_with_stop(*args)
        self.assertEqual(session.calls, [])

    def test_the_stop_price_is_rounded_to_cents(self):
        api, session = client(FakeResponse(200, {}))
        api.submit_entry_with_stop("QQQ", 10, 199.987654)
        self.assertEqual(session.calls[0]["json"]["stop_loss"]["stop_price"], "199.99")


class AccountAndFillsTests(unittest.TestCase):
    def test_one_fund_is_sold_at_a_time_never_everything(self):
        api, session = client(FakeResponse(200, {"id": "x"}))
        api.close_position("spy")
        call = session.calls[0]
        self.assertEqual((call["method"], call["url"]), ("DELETE", PAPER_URL + "/v2/positions/SPY"))
        self.assertFalse(hasattr(api, "close_all_positions"))

    def test_a_stop_is_cancelled_by_its_id(self):
        api, session = client(FakeResponse(204, None))
        api.cancel_order("61e69015-8549-4bfd-b9c3-01e75843f47d")
        self.assertEqual((session.calls[0]["method"], session.calls[0]["url"]),
                         ("DELETE", PAPER_URL + "/v2/orders/61e69015-8549-4bfd-b9c3-01e75843f47d"))

    def test_bad_ids_and_symbols_are_refused_before_any_request(self):
        api, session = client()
        for call, arg in ((api.cancel_order, ""), (api.cancel_order, "../positions"), (api.cancel_order, "a b"),
                          (api.close_position, ""), (api.close_position, "SPY/../x"), (api.close_position, "1234567")):
            with self.assertRaises(BrokerError):
                call(arg)
        self.assertEqual(session.calls, [])

    def test_the_pattern_day_trading_rule_gets_its_own_message(self):
        api, _ = client(FakeResponse(403, {"message": "trade denied due to pattern day trading protection"}))
        with self.assertRaises(BrokerError) as caught:
            api.submit_entry_with_stop("SPY", 5, 100.0)
        self.assertIn("pattern day trading", str(caught.exception))
        self.assertNotIn("rejected the paper keys", str(caught.exception))

    def test_closed_orders_roll_legs_up_and_skip_unfilled_ones(self):
        api, session = client(FakeResponse(200, [
            {"symbol": "SPY", "side": "buy", "filled_qty": "100", "filled_avg_price": "500.5", "type": "market",
             "filled_at": "2026-10-05T13:51:00Z",
             "legs": [{"symbol": "SPY", "side": "sell", "filled_qty": "100", "filled_avg_price": "499.9",
                       "type": "stop", "filled_at": "2026-10-05T15:00:00Z"},
                      {"symbol": "SPY", "side": "sell", "filled_qty": "0", "filled_avg_price": None,
                       "type": "limit", "filled_at": None}]},
            {"symbol": "QQQ", "side": "buy", "filled_qty": "0", "type": "market"},
            "junk"]))
        fills = api.closed_orders("2026-10-05T00:00:00-04:00")
        self.assertEqual([(f["symbol"], f["side"], f["qty"], f["price"], f["type"]) for f in fills],
                         [("SPY", "buy", 100.0, 500.5, "market"), ("SPY", "sell", 100.0, 499.9, "stop")])
        params = session.calls[0]["params"]
        self.assertEqual((params["status"], params["nested"], params["direction"]), ("closed", "true", "asc"))

    def test_account_exposes_which_account_it_is(self):
        api, _ = client(FakeResponse(200, {"id": "abc-123", "cash": "1", "equity": "1"}))
        self.assertEqual(api.account()["account_id"], "abc-123")
        api, _ = client(FakeResponse(200, {"account_number": "PA999", "cash": "1", "equity": "1"}))
        self.assertEqual(api.account()["account_id"], "PA999")
        api, _ = client(FakeResponse(200, {"cash": "1", "equity": "1"}))
        self.assertEqual(api.account()["account_id"], "")


if __name__ == "__main__":
    unittest.main()
