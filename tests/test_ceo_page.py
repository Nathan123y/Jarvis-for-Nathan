import json
import tempfile
import unittest
from pathlib import Path

from worker import ceo


class CeoPage(unittest.TestCase):
    def test_missing_data_still_gives_a_page(self):
        with tempfile.TemporaryDirectory() as d:
            text = ceo.page(now=1_800_000_000, base=Path(d), attention=[])
        self.assertIn("CEO page", text)
        self.assertIn("Needs you: nothing.", text)

    def test_shop_counts_recent_sales(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "memory").mkdir()
            now = 1_800_000_000
            (Path(d) / "memory" / "etsy_shop.json").write_text(json.dumps({
                "designs": [{"made": now - 100}, {"made": now - 9 * 86400}],
                "sales_log": [{"at": now - 3600, "channel": "ebay", "price": 22.0}]}))
            line = ceo.shop(now, Path(d))
        self.assertIn("1 new designs", line)
        self.assertIn("Sales 7d: 1 ($22)", line)


if __name__ == "__main__":
    unittest.main()
