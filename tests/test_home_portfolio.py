import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from worker import home_portfolio as hp

NY = ZoneInfo("America/New_York")
CJ = json.dumps({"underlying": "IWM", "right": "P", "strike": "270", "expiration": "2026-11-23"})


def snap(equity, positions=()):
    return json.dumps({"equity": str(equity), "starting_cash": "5000", "positions": list(positions), "per_bot": {"iwm-drive": {}, "msft-drive": {}}})


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "paper.db"
        con = sqlite3.connect(self.db)
        con.execute("create table fills (seq integer primary key, bot_id, side, contract_id, contract_json, quantity, price, ts)")
        con.execute("create table events (seq integer primary key, type, ts, payload_json)")
        con.executemany("insert into fills (bot_id, side, contract_id, contract_json, quantity, price, ts) values (?,?,?,?,?,?,?)", [
            ("iwm-drive", "BUY_TO_OPEN", "IWM1", CJ, 3, 2.43, "2026-11-19T14:45:10Z"),
            ("iwm-drive", "BUY_TO_OPEN", "IWM1", CJ, 3, 2.43, "2026-11-19T14:45:11Z"),
            ("iwm-drive", "SELL_TO_CLOSE", "IWM1", CJ, 6, 4.61, "2026-11-19T17:50:21Z")])
        pos = {"bot_id": "iwm-drive", "quantity": 2, "avg_price": "2.00", "mark": "2.50", "unrealized": "100",
               "contract": {"underlying": "IWM", "right": "C", "strike": "265", "expiration": "2026-11-25"}}
        con.executemany("insert into events (type, ts, payload_json) values ('ACCOUNT_SNAPSHOT', ?, ?)", [
            ("2026-11-19T21:00:00Z", snap(6308)), ("2026-11-20T15:00:00Z", snap(6408, [pos]))])
        con.commit()
        con.close()
        (self.dir / "status.json").write_text(json.dumps({"equity": 960.0, "first_equity": 1000, "day_start_equity": 950, "positions": [], "notes": ["16:38 in cash"]}))
        (self.dir / "trades.jsonl").write_text(
            '{"ts": "2026-10-31T12:44:53+00:00", "kind": "buy", "symbol": "PEPE/USD", "usd": 292.6, "price": 5.04e-06}\n'
            '{"ts": "2026-11-06T00:05:10+00:00", "kind": "sell", "symbol": "PEPE/USD", "price": 4.8e-06, "spent": 292.6, "pnl": -20.7}\n')

    def pf(self, now):
        return hp.portfolio(now, self.db, self.dir / "status.json", self.dir / "trades.jsonl")

    def test_a_brokerage_style_summary(self):
        p = self.pf(datetime(2026, 11, 20, 10, 5, tzinfo=NY))
        o = p["options"]
        self.assertEqual((o["equity"], o["all_time"], o["today"], o["trades"], o["win_rate"]), (6408, 1408, 100, 1, 100))
        self.assertEqual(p["total"], 6408 + 960)
        self.assertEqual(p["positions"][0]["name"], "IWM $265 Call · Nov 25")
        self.assertEqual([h["name"] for h in p["history"]], ["IWM $270 Put · Nov 23", "PEPE"])   # newest first
        self.assertEqual(p["history"][0]["qty"], 6)
        self.assertEqual(p["history"][0]["pnl"], 1308)
        self.assertEqual({s["name"] for s in p["strategies"]}, {"IWM · Open drive", "MSFT · Open drive"})

    def test_overnight_there_are_no_positions_and_no_today(self):
        p = self.pf(datetime(2026, 11, 21, 9, tzinfo=NY))
        self.assertEqual(p["positions"], [])
        self.assertEqual(p["options"]["today"], 0)

    def test_market_hours_know_weekends_holidays_and_half_days(self):
        self.assertTrue(hp.market(datetime(2026, 11, 24, 10, tzinfo=NY))["open"])
        thanksgiving = hp.market(datetime(2026, 11, 26, 11, tzinfo=NY))
        self.assertFalse(thanksgiving["open"])
        self.assertIn("holiday", thanksgiving["label"])
        self.assertFalse(hp.market(datetime(2026, 11, 27, 13, 30, tzinfo=NY))["open"])      # closes at 1 PM the day after
        self.assertFalse(hp.market(datetime(2026, 11, 28, 11, tzinfo=NY))["open"])          # Saturday

    def test_missing_files_are_not_errors(self):
        p = hp.portfolio(datetime(2026, 11, 20, 10, tzinfo=NY), Path("/nope/paper.db"), Path("/nope/s.json"), Path("/nope/t.jsonl"))
        self.assertEqual(p["history"], [])
        self.assertFalse(p["crypto"]["ok"])


class ProjectionTests(unittest.TestCase):
    def trips(self, pcts, start=datetime(2026, 11, 16, 9, 45, tzinfo=NY)):
        from datetime import timedelta
        out = []
        for i, (bot, pct) in enumerate(pcts):
            t = start + timedelta(days=i // 2)
            out.append({"bot": bot, "pct": pct, "pnl": pct * 10, "entry": 10.0, "qty": 1, "opened": t, "closed": t})
        return out

    def test_a_lucky_start_is_pulled_toward_the_backtest(self):
        lucky = self.trips([("qqq-drive", 50.0)] * 10)
        j = hp.projection(lucky, datetime(2026, 11, 24, 17, tzinfo=NY), paths=500)
        self.assertLess(j["per_trade_pct"], 50.0 * 10 / 40 + 4.0)      # 10 live trades vs 30 of backtest weight
        self.assertGreater(j["per_trade_pct"], 4.0)
        self.assertEqual(j["live_weight"], 25)

    def test_losing_live_trades_make_the_projection_negative(self):
        bad = self.trips([("qqq-drive", -30.0)] * 40)
        j = hp.projection(bad, datetime(2026, 12, 15, 17, tzinfo=NY), paths=500)
        self.assertLess(j["next"]["median"], 0)
        self.assertLess(j["next"]["chance_up"], 50)

    def test_the_range_brackets_the_middle_and_trades_before_the_lineup_are_ignored(self):
        old = self.trips([("qqq-orb-day", -90.0)] * 6, start=datetime(2026, 11, 3, 9, 45, tzinfo=NY))
        new = self.trips([("spy-drive", 20.0), ("spy-drive", -30.0), ("iwm-orb-scalp", 10.0)] * 4)
        j = hp.projection(old + new, datetime(2026, 11, 24, 17, tzinfo=NY), paths=500)
        self.assertEqual(j["trades"], 12)
        n = j["next"]
        self.assertLessEqual(n["low"], n["median"])
        self.assertLessEqual(n["median"], n["high"])

    def test_growing_limits_make_bigger_trades_than_fixed_ones_on_a_bigger_account(self):
        trips = self.trips([("spy-drive", 20.0), ("spy-drive", -30.0), ("qqq-drive", 15.0)] * 5)
        fixed = {"trade_pct": None, "open_pct": None, "stop_pct": None, "trade_cap": 1500.0, "open_cap": 4200.0, "stop_cap": 600.0, "max_contracts": 25}
        grow = {**fixed, "trade_pct": 0.30, "open_pct": 0.84, "stop_pct": 0.12}
        now = datetime(2026, 11, 24, 17, tzinfo=NY)
        a = hp.projection(trips, now, paths=400, equity=20000, limits=fixed)
        b = hp.projection(trips, now, paths=400, equity=20000, limits=grow)
        self.assertGreater(b["cost"], a["cost"])                        # 30% of $20,000 buys more than the $1,500 cap
        self.assertGreater(abs(b["next"]["high"] - b["next"]["low"]), abs(a["next"]["high"] - a["next"]["low"]))
        self.assertTrue(b["grows"])

    def test_the_review_horizon_counts_trading_days_only(self):
        self.assertEqual(hp.trading_days_between(date(2026, 11, 24), date(2026, 11, 30)), 3)   # Wed, Fri (Thanksgiving off), Mon


if __name__ == "__main__":
    unittest.main()
