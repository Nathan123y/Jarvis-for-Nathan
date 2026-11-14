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

    def test_snapshot_never_breaks_without_any_files(self):
        snap = hq.snapshot()
        self.assertIn("summary", snap)
        self.assertEqual(snap["summary"]["agents"], sum(len(c["agents"]) for c in snap["crews"]))


if __name__ == "__main__":
    unittest.main()
