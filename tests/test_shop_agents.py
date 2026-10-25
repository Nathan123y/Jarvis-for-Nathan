import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import worker.shop as ws
from worker import shop_agents as sa
from tests.test_etsy_shop import FakeClients, _Base


class ThinkingClients(FakeClients):
    """FakeClients that can also 'think' (the agents' Gemini calls), with canned answers."""

    def __init__(self, *a, answers=None, **k):
        super().__init__(*a, **k)
        self.answers, self.asked = list(answers or []), []

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else None


class NotebookTests(unittest.TestCase):
    def test_scout_picks_once_a_day_and_falls_back_to_the_raw_feeds(self):
        nb = sa.load(Path("/nonexistent/nb.json"))
        asks = []
        ask = lambda p: asks.append(p) or {"picks": [{"phrase": "Black Cat Clapping", "where": "KYM", "why": "x", "angle": "y"}]}
        text = sa.scout(nb, "KYM:\n- Black Cat Clapping", ask, "2026-10-25")
        self.assertIn('"Black Cat Clapping"', text)
        sa.scout(nb, "KYM:\n- Black Cat Clapping", ask, "2026-10-25")
        self.assertEqual(len(asks), 1)
        self.assertEqual(nb["feed"][-1]["from"], "scout")
        nb2 = sa.load(Path("/nonexistent/nb.json"))
        self.assertEqual(sa.scout(nb2, "raw feeds", lambda p: None, "2026-10-25"), "raw feeds")

    def test_winners_take_turns_with_fresh_trends(self):
        nb = sa.load(Path("/nonexistent/nb.json"))
        self.assertIsNone(sa.next_spinoff(nb, False, 0))                       # no winner yet: trends only
        nb["winners"] = [{"design": "a", "sales": 3, "at": 1}, {"design": "b", "sales": 1, "at": 2}]
        self.assertEqual(sa.next_spinoff(nb, False, 0), "a")
        self.assertIsNone(sa.next_spinoff(nb, True, 1))                        # the day after a spin-off: trends
        self.assertEqual(sa.next_spinoff(nb, False, 1), "b")

    def test_the_analyst_rewrites_the_notebook_from_results(self):
        nb = sa.load(Path("/nonexistent/nb.json"))
        nb["lessons"] = ["old lesson"]
        sa.problem(nb, "Designer's idea was refused: protected name (jordans)", clock=lambda: 5)
        st = {"designs": [{"id": "a", "slogan": "MY DAILY DRIVER IS ANXIETY", "niche": "adulting", "made": 0,
                           "products": {"tee": {"published": True}}}]}
        nb["winners"] = [{"design": "a", "slogan": "MY DAILY DRIVER IS ANXIETY", "sales": 2, "revenue": 45.98}]
        seen = []
        ok = sa.review(nb, st, lambda p: seen.append(p) or {"lessons": ["Anxiety jokes sell", "Avoid brand names"],
                                                           "double_down": ["adulting anxiety"], "avoid": []}, clock=lambda: 10)
        self.assertTrue(ok)
        self.assertIn("SOLD 2 ($45.98)", seen[0])
        self.assertIn("protected name (jordans)", seen[0])
        self.assertEqual(nb["lessons"], ["Anxiety jokes sell", "Avoid brand names"])
        self.assertIn("Make more of: adulting anxiety", sa.lessons_text(nb))
        self.assertFalse(sa.review(nb, st, lambda p: None))                    # no answer: the old notes stay
        self.assertEqual(nb["lessons"], ["Anxiety jokes sell", "Avoid brand names"])


class CycleTests(_Base):
    def test_a_sale_turns_into_spin_offs_that_the_designer_makes_from_the_winner(self):
        c = ThinkingClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)))
        rec = s.make_design()
        st = ws.load_state(self.dir / "s.json")
        st["make_spinoff"] = [rec["id"]]
        ws.save_state(st, self.dir / "s.json")
        with patch.object(ws, "settings", lambda: dict(ws.DEFAULTS, designs_per_day=0)), \
                patch("worker.storefront.publish", lambda *a, **k: ""):
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=12))
        st = ws.load_state(self.dir / "s.json")
        self.assertEqual(st["designs"][-1]["spinoff_of"], rec["id"])
        self.assertEqual(st["make_spinoff"], [])
        self.assertIn("SPIN-OFF OF A WINNER", c.notes)
        self.assertEqual(c.trending, "")                                       # a spin-off ignores the trend list
        feed = sa.load(self.dir / "notebook.json")["feed"]
        self.assertIn("a spin-off of", feed[-1]["text"])

    def test_the_evening_review_runs_once_after_nine(self):
        c = ThinkingClients(answers=[{"lessons": ["Short slogans read better"], "double_down": [], "avoid": []}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None)
        day = datetime(2026, 10, 25)
        self.assertFalse(svc.evening_review(s, day.replace(hour=20)))
        self.assertTrue(svc.evening_review(s, day.replace(hour=21, minute=5)))
        self.assertFalse(svc.evening_review(s, day.replace(hour=22)))
        self.assertEqual(sa.load(self.dir / "notebook.json")["lessons"], ["Short slogans read better"])


if __name__ == "__main__":
    unittest.main()
