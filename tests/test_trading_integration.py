"""The real Alpaca client and the real runner, joined, against a stand-in server.

Nothing here touches the network: a fake session answers by URL path, in the
shapes Alpaca documents (numbers as strings, bars keyed by symbol, and so on).
"""
import tempfile
import unittest
from pathlib import Path

from trading.broker import DATA_URL, PAPER_URL, AlpacaPaper
from trading.journal import Journal
from trading.runner import Runner

from trading_fakes import RISING, FakeResponse, make_bars

DAY = "2026-10-06"


class StandInAlpaca:
    def __init__(self):
        self.calls, self.cash, self.held = [], 100_000.0, {}
        self.bars = make_bars(DAY, drifts=RISING)

    def request(self, method, url, headers=None, params=None, json=None, **kw):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params,
                           "json": json, "kw": kw})
        path = url.replace(PAPER_URL, "").replace(DATA_URL, "")
        if path == "/v2/clock":
            return FakeResponse(200, {"timestamp": f"{DAY}T10:30:00.123456789-04:00", "is_open": True,
                                      "next_open": "2026-10-07T09:30:00-04:00",
                                      "next_close": f"{DAY}T16:00:00-04:00"})
        if path == "/v2/account":
            equity = self.cash + sum(self.held.values())
            return FakeResponse(200, {"status": "ACTIVE", "cash": str(self.cash), "equity": str(equity),
                                      "last_equity": "100000", "buying_power": str(self.cash * 2),
                                      "trading_blocked": False, "account_blocked": False})
        if path == "/v2/positions":
            return FakeResponse(200, [{"symbol": s, "qty": str(v / 100), "market_value": str(v),
                                       "current_price": "100", "side": "long"}
                                      for s, v in self.held.items()])
        if path == "/v2/orders" and method == "GET":
            return FakeResponse(200, [])
        if path == "/v2/orders" and method == "POST":
            self.cash -= float(json["notional"])
            self.held[json["symbol"]] = self.held.get(json["symbol"], 0.0) + float(json["notional"])
            return FakeResponse(200, {"id": "abc", "status": "accepted",
                                      "client_order_id": json.get("client_order_id", "")})
        if path == "/v2/stocks/trades/latest":
            return FakeResponse(200, {"trades": {"SPY": {"p": 500.0, "t": "x"}}})
        if path == "/v2/stocks/bars":
            wanted = params["symbols"].split(",")
            return FakeResponse(200, {"bars": {s: [{"t": f"{d}T04:00:00Z", "c": c}
                                                    for d, c in self.bars[s]] for s in wanted},
                                      "next_page_token": None})
        return FakeResponse(404, {"message": f"unexpected {method} {path}"})


class EndToEndTests(unittest.TestCase):
    def test_a_full_trading_day_through_the_real_client(self):
        server = StandInAlpaca()
        broker = AlpacaPaper("PKTEST", "SECRETTEST", session=server)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp))
            runner = Runner(broker, journal, log=lambda _m: None, sleep=lambda _s: None, fill_wait=0.0)
            self.assertEqual(runner.step(), "rebalanced")
            self.assertEqual(runner.step(), "already_ran")

        posts = [c for c in server.calls if c["method"] == "POST"]
        self.assertEqual(sorted(c["json"]["symbol"] for c in posts), ["GLD", "QQQ", "SPY"])
        for post in posts:
            self.assertTrue(post["url"].startswith(PAPER_URL + "/v2/orders"))
            self.assertEqual(post["json"]["type"], "market")
            self.assertEqual(post["json"]["time_in_force"], "day")
            self.assertTrue(post["json"]["client_order_id"].startswith("jv-20261006-"))
        self.assertTrue(all(c["url"].startswith((PAPER_URL, DATA_URL)) for c in server.calls))
        self.assertTrue(all(c["headers"]["APCA-API-KEY-ID"] == "PKTEST" for c in server.calls))
        self.assertTrue(all(c["kw"].get("allow_redirects") is False for c in server.calls))
        self.assertGreaterEqual(server.cash, 0.0)
        self.assertAlmostEqual(server.cash, 100_000 * 0.02, delta=0.05)


if __name__ == "__main__":
    unittest.main()
