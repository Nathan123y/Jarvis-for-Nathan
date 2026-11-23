import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from worker import home_chart as hc

NY = ZoneInfo("America/New_York")
CJ = json.dumps({"underlying": "IWM", "right": "P", "strike": "270"})


def make_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute("create table fills (seq integer primary key, bot_id, side, contract_id, contract_json, quantity, price, ts, client_order_id)")
    con.executemany("insert into fills (bot_id, side, contract_id, contract_json, quantity, price, ts, client_order_id) values (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


ROWS = [("iwm-drive", "BUY_TO_OPEN", "IWM1", CJ, 3, 2.43, "2026-11-19T14:45:10.50Z", "a"),
        ("iwm-drive", "BUY_TO_OPEN", "IWM1", CJ, 3, 2.43, "2026-11-19T14:45:10.61Z", "a"),     # the same order, filled in two pieces
        ("iwm-drive", "SELL_TO_CLOSE", "IWM1", CJ, 6, 4.61, "2026-11-19T17:50:21Z", "b"),
        ("iwm-drive", "BUY_TO_OPEN", "IWM1", CJ, 1, 2.00, "2026-11-20T14:45:00Z", "c")]       # another day


def fake_fetch(params):
    start = datetime.fromisoformat(params["start"])
    return [{"t": datetime.fromtimestamp(start.timestamp() + 60 * i, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 265, "h": 266, "l": 264, "c": 265.5} for i in range(390)]


class HomeChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "paper.db"
        make_db(self.db, ROWS)
        hc._cache.clear()

    def test_a_split_order_is_one_move_and_the_sale_carries_its_profit(self):
        m = hc.moves(date(2026, 11, 19), self.db)
        self.assertEqual([(x["side"], x["qty"]) for x in m], [("buy", 6), ("sell", 6)])
        self.assertEqual(m[1]["pnl"], 1308)
        self.assertEqual(m[1]["pct"], 89.7)
        self.assertEqual(m[0]["kind"], "put")

    def test_markers_sit_on_the_candle_of_their_minute(self):
        c = hc.chart("IWM", "2026-11-19", now=datetime(2026, 11, 23, 12, tzinfo=NY), fetch=fake_fetch, db=self.db)
        self.assertEqual(len(c["bars"]), 390)
        buy = c["marks"][0]
        self.assertEqual(buy["bar"], int(datetime(2026, 11, 19, 9, 45, tzinfo=NY).timestamp()))
        self.assertEqual(c["counts"]["IWM"], 1)
        self.assertFalse(c["live"])

    def test_without_a_symbol_it_shows_the_one_the_bots_last_traded(self):
        c = hc.chart(None, "2026-11-19", now=datetime(2026, 11, 23, 12, tzinfo=NY), fetch=fake_fetch, db=self.db)
        self.assertEqual(c["symbol"], "IWM")

    def test_weekends_walk_back_to_the_last_trading_day(self):
        c = hc.chart("SPY", None, now=datetime(2026, 11, 22, 12, tzinfo=NY), fetch=fake_fetch, db=self.db)
        self.assertEqual(c["day"], "2026-11-20")

    def test_price_trouble_is_reported_not_raised(self):
        def broken(params):
            raise OSError("no network")
        c = hc.chart("SPY", "2026-11-19", now=datetime(2026, 11, 23, 12, tzinfo=NY), fetch=broken, db=self.db)
        self.assertIn("couldn't load prices", c["error"])
        self.assertEqual(c["bars"], [])

    def test_no_database_means_no_moves(self):
        self.assertEqual(hc.moves(date(2026, 11, 19), Path("/nonexistent/paper.db")), [])


if __name__ == "__main__":
    unittest.main()
