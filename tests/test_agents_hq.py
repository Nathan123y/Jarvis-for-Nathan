import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from worker import agents_hq as hq

LOG = """2026-11-13 19:00:00,100 INFO jarvis.shop shop: designed 'Ugly Sweater' (I'M TOTALLY PINE)
2026-11-13 19:05:00,100 INFO jarvis.alerts phone alert sent: 🛍️ Just listed: Ugly Christmas Sweater
2026-11-13 19:06:00,100 INFO jarvis.shop storefront: published 123 designs to https://example.com/
2026-11-13 19:07:00,100 INFO jarvis.viral viral: TikTok upload failed (timeout)
Traceback (most recent call last):
  File "x.py", line 1
2026-11-13 09:00:00,100 INFO jarvis.guides guides: made a video
"""


class AgentsHQTests(unittest.TestCase):
    def lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.log"
            p.write_text(LOG)
            return hq.read_log(p)

    def test_log_lines_parse_and_tracebacks_are_skipped(self):
        self.assertEqual(len(self.lines()), 5)

    def test_each_agent_gets_its_own_last_activity_and_light(self):
        crews = {c["crew"]: {a["name"]: a for a in c["agents"]}
                 for c in hq.agents_status(self.lines(), datetime(2026, 11, 13, 19, 10))}
        shop = crews["Shop crew"]
        self.assertIn("I'M TOTALLY PINE", shop["Designer"]["last"])           # not the storefront's "published 123 designs"
        self.assertEqual(shop["Designer"]["light"], "green")
        self.assertIn("published 123 designs", shop["Storefront"]["last"])
        self.assertIn("Just listed", shop["Publisher"]["last"])                 # found in the alerts log
        self.assertEqual(shop["Trend Scout"]["light"], "grey")
        self.assertTrue(crews["Video crew"]["TikTok poster"]["problem"])       # a failure is flagged
        self.assertEqual(crews["Game guides crew"]["Guide maker"]["light"], "blue")   # ten hours ago: active in 24 h

    def test_an_inbox_failure_is_the_inbox_readers_and_clears_once_it_works(self):
        log = ("2026-11-23 05:45:16,939 INFO jarvis.internships internships: the Scout found 0 new Summer 2027 postings\n"
               "2026-11-23 15:04:35,215 INFO jarvis.internships internships: reply check failed (ServerNotFoundError)\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.log"
            p.write_text(log)
            crew = {a["name"]: a for c in hq.agents_status(hq.read_log(p), datetime(2026, 11, 23, 16, 0))
                    if c["crew"] == "Internship crew" for a in c["agents"]}
            self.assertIn("Scout found 0", crew["Scout"]["last"])               # "NotFound" isn't the Scout's
            self.assertTrue(crew["Inbox reader"]["problem"])
            p.write_text(log + "2026-11-24 07:00:01,000 INFO jarvis.internships internships: reply check ok\n")
            crew = {a["name"]: a for c in hq.agents_status(hq.read_log(p), datetime(2026, 11, 24, 7, 5))
                    if c["crew"] == "Internship crew" for a in c["agents"]}
            self.assertFalse(crew["Inbox reader"]["problem"])

    def test_snapshot_never_breaks_without_any_files(self):
        snap = hq.snapshot()
        self.assertIn("summary", snap)
        self.assertEqual(snap["summary"]["agents"], sum(len(c["agents"]) for c in snap["crews"]))

    def test_an_order_filled_in_pieces_is_one_trade(self):
        cj = '{"underlying": "IWM", "right": "P"}'
        rows = [("iwm-drive", "BUY_TO_OPEN", "IWM1", cj, 3, 2.43, "2026-11-19T14:45:10.50Z"),
                ("iwm-drive", "BUY_TO_OPEN", "IWM1", cj, 3, 2.43, "2026-11-19T14:45:10.61Z"),
                ("iwm-orb-scalp", "BUY_TO_OPEN", "IWM1", cj, 5, 2.58, "2026-11-19T14:55:06Z"),
                ("iwm-orb-scalp", "SELL_TO_CLOSE", "IWM1", cj, 5, 4.41, "2026-11-19T16:54:08Z"),
                ("iwm-drive", "SELL_TO_CLOSE", "IWM1", cj, 6, 4.61, "2026-11-19T17:50:21Z")]
        trades = {t["bot"]: t for t in hq.round_trips(rows)}
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades["iwm-drive"]["qty"], 6)
        self.assertAlmostEqual(trades["iwm-drive"]["pnl"], 1308)
        self.assertEqual(trades["iwm-drive"]["kind"], "put")

    def test_messages_lose_the_programmer_noise(self):
        self.assertEqual(hq.plain("Acme failed: TimeoutError('launch: Timeout 180000ms exceeded.\\nCall log:\\n /Users/x')"),
                         "Acme failed: timed out")
        self.assertEqual(hq.bot_label("qqq-orb-scalp"), "QQQ opening-range scalp")

    def test_trading_without_a_database_is_not_an_error(self):
        self.assertEqual(hq.trading(db=Path("/nonexistent/paper.db")), {"ok": False})


if __name__ == "__main__":
    unittest.main()
