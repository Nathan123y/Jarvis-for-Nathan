import unittest

from trading.broker import DATA_URL, PAPER_URL, AlpacaPaper, BrokerError

from trading_fakes import FakeResponse, FakeSession

KEY, SECRET = "PKTESTKEY1234", "s3cr3t-value-xyz"


def client(*responses):
    session = FakeSession(*responses)
    return AlpacaPaper(KEY, SECRET, session=session), session


class SafetyTests(unittest.TestCase):
    def test_only_the_paper_address_is_accepted(self):
        with self.assertRaises(ValueError):
            AlpacaPaper(KEY, SECRET, session=FakeSession(), trading_url="https://api.alpaca.markets")
        with self.assertRaises(ValueError):
            AlpacaPaper(KEY, SECRET, session=FakeSession(), data_url="https://example.com")
        AlpacaPaper(KEY, SECRET, session=FakeSession(), trading_url=PAPER_URL + "/")

    def test_missing_keys_are_a_friendly_error(self):
        for key, secret in (("", SECRET), (KEY, ""), (None, None), ("  ", " ")):
            with self.assertRaises(BrokerError):
                AlpacaPaper(key, secret, session=FakeSession())

    def test_keys_travel_in_headers_only_and_redirects_are_not_followed(self):
        api, session = client(FakeResponse(200, {"cash": "1", "equity": "1"}))
        api.account()
        call = session.calls[0]
        self.assertEqual(call["headers"]["APCA-API-KEY-ID"], KEY)
        self.assertEqual(call["headers"]["APCA-API-SECRET-KEY"], SECRET)
        self.assertFalse(call["allow_redirects"])
        self.assertTrue(call["url"].startswith(PAPER_URL))
        self.assertNotIn(KEY, call["url"] + str(call.get("params")))

    def test_errors_never_contain_the_keys(self):
        leaky = FakeResponse(422, {"message": f"bad request for {KEY} / {SECRET}"})
        api, _ = client(leaky)
        with self.assertRaises(BrokerError) as caught:
            api.submit_market_order("SPY", "buy", notional=100)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        api, _ = client(RuntimeError(f"connection failed {SECRET}"))
        with self.assertRaises(BrokerError) as caught:
            api.account()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_status_codes_become_plain_sentences(self):
        for status, fragment in ((401, "rejected"), (403, "rejected"), (429, "rate-limiting"), (503, "trouble")):
            api, _ = client(FakeResponse(status, {}))
            with self.assertRaises(BrokerError) as caught:
                api.account()
            self.assertIn(fragment, str(caught.exception))

    def test_unreadable_reply_is_handled(self):
        api, _ = client(FakeResponse(200, ValueError("not json")))
        with self.assertRaises(BrokerError):
            api.account()


class ParsingTests(unittest.TestCase):
    def test_account_accepts_numbers_sent_as_strings(self):
        api, _ = client(FakeResponse(200, {"status": "ACTIVE", "cash": "99000.50", "equity": "100000",
                                           "last_equity": "99500", "buying_power": "200000",
                                           "trading_blocked": False, "account_blocked": False}))
        account = api.account()
        self.assertEqual((account["cash"], account["equity"], account["last_equity"]),
                         (99000.5, 100000.0, 99500.0))
        self.assertFalse(account["blocked"])

    def test_blocked_flags_are_noticed_and_garbage_numbers_do_not_crash(self):
        api, _ = client(FakeResponse(200, {"cash": "nan", "equity": None, "trading_blocked": True}))
        account = api.account()
        self.assertTrue(account["blocked"])
        self.assertEqual(account["cash"], 0.0)

    def test_positions_keep_the_exact_share_text(self):
        api, _ = client(FakeResponse(200, [{"symbol": "spy", "qty": "12.345678", "market_value": "6000.1",
                                            "current_price": "486"}]))
        position = api.positions()["SPY"]
        self.assertEqual(position["qty_text"], "12.345678")
        self.assertEqual(position["market_value"], 6000.1)

    def test_latest_prices_accepts_either_reply_shape(self):
        api, _ = client(FakeResponse(200, {"trades": {"SPY": {"p": 501.25, "t": "x"}}}))
        self.assertEqual(api.latest_prices(["SPY"]), {"SPY": 501.25})
        api, session = client(FakeResponse(200, {"SPY": {"p": 502.0}, "QQQ": {"p": 0}}))
        self.assertEqual(api.latest_prices(["SPY", "QQQ"]), {"SPY": 502.0})
        self.assertEqual(session.calls[0]["params"]["feed"], "iex")
        self.assertTrue(session.calls[0]["url"].startswith(DATA_URL))

    def test_daily_bars_follow_pages_and_use_the_free_feed(self):
        page1 = FakeResponse(200, {"bars": {"SPY": [{"t": "2026-01-02T05:00:00Z", "c": 100.0}]},
                                   "next_page_token": "abc"})
        page2 = FakeResponse(200, {"bars": {"SPY": [{"t": "2026-01-05T05:00:00Z", "c": 101.5}]},
                                   "next_page_token": None})
        api, session = client(page1, page2)
        bars = api.daily_bars(["SPY"], "2026-01-01")
        self.assertEqual(bars["SPY"], [("2026-01-02", 100.0), ("2026-01-05", 101.5)])
        self.assertEqual(session.calls[1]["params"]["page_token"], "abc")
        self.assertEqual(session.calls[0]["params"]["feed"], "iex")
        self.assertEqual(session.calls[0]["params"]["adjustment"], "all")


class OrderTests(unittest.TestCase):
    def test_dollar_buy_is_a_day_market_order(self):
        api, session = client(FakeResponse(200, {"id": "o1", "status": "accepted", "client_order_id": "c1"}))
        result = api.submit_market_order("QQQ", "buy", notional=33333.333, client_order_id="c1")
        body = session.calls[0]["json"]
        self.assertEqual(body, {"symbol": "QQQ", "side": "buy", "type": "market",
                                "time_in_force": "day", "notional": "33333.33", "client_order_id": "c1"})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(session.calls[0]["method"], "POST")

    def test_share_sell_passes_the_quantity_text_through(self):
        api, session = client(FakeResponse(200, {"id": "o2", "status": "accepted"}))
        api.submit_market_order("TLT", "sell", qty="12.345678")
        self.assertEqual(session.calls[0]["json"]["qty"], "12.345678")
        self.assertNotIn("notional", session.calls[0]["json"])

    def test_bad_orders_are_refused_before_any_request(self):
        api, session = client()
        for args, kwargs in ((("SPY; DROP", "buy"), {"notional": 1}), (("SPY", "short"), {"notional": 1}),
                             (("SPY", "buy"), {}), (("SPY", "buy"), {"qty": "1", "notional": 5})):
            with self.assertRaises(BrokerError):
                api.submit_market_order(*args, **kwargs)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
