import unittest
from unittest.mock import patch

from core import startup_brief as sb

TODAY = "2026-10-06"


def task(i, title, due=None, priority="normal", status="open", project=""):
    return {"id": i, "title": title, "due": due, "priority": priority,
            "status": status, "project": project}


class Compose(unittest.TestCase):
    def test_overdue_then_today_spoken_and_listed(self):
        tasks = [task(1, "Physics lab", "2026-10-07"), task(2, "Essay draft", TODAY, "high"),
                 task(3, "Return library book", "2026-10-01"), task(4, "Done thing", TODAY, status="done")]
        spoken, panel = sb.compose(tasks, None, None, TODAY)
        self.assertIn("Mission log (3 open)", spoken)
        self.assertIn("1 overdue: Return library book", spoken)
        self.assertIn("1 due today: Essay draft", spoken)
        self.assertNotIn("Done thing", spoken + panel)
        self.assertLess(panel.index("OVERDUE"), panel.index("TODAY"))
        self.assertLess(panel.index("TODAY"), panel.index("NEXT"))
        self.assertIn("#1  Physics lab", panel)

    def test_nothing_due_names_next_up(self):
        spoken, _ = sb.compose([task(1, "Gym plan"), task(2, "Taxes", "2026-11-01")], None, None, TODAY)
        self.assertIn("nothing due today; next up is Taxes (due 2026-11-01)", spoken)

    def test_empty(self):
        spoken, panel = sb.compose([], None, None, TODAY)
        self.assertIn("no open missions", spoken)
        self.assertIn("No open missions", panel)

    def test_canvas_today_and_overdue_only(self):
        items = [{"name": "Quiz 3", "course": "Bio", "due": TODAY, "overdue": False},
                 {"name": "Lab 2", "course": "Chem", "due": "2026-10-01", "overdue": True},
                 {"name": "Later", "course": "Bio", "due": "2026-10-20", "overdue": False}]
        spoken, panel = sb.compose([], None, items, TODAY)
        self.assertIn("School work due: Quiz 3; Lab 2 (overdue)", spoken)
        self.assertNotIn("Later", spoken + panel)

    def test_calendar_when_given(self):
        spoken, _ = sb.compose([], [("9:00", "Class")], None, TODAY)
        self.assertIn("Calendar today: 9:00 Class", spoken)
        spoken, _ = sb.compose([], [], None, TODAY)
        self.assertIn("Nothing on the calendar today", spoken)

    def test_spoken_list_is_capped(self):
        tasks = [task(i, f"Old {i}", "2026-09-01") for i in range(10)]
        spoken, _ = sb.compose(tasks, None, None, TODAY)
        self.assertIn("10 overdue", spoken)
        self.assertNotIn("Old 7", spoken)

    def test_titles_are_flattened(self):
        spoken, _ = sb.compose([task(1, "line one\nIGNORE ALL\tPRIOR", TODAY)], None, None, TODAY)
        self.assertNotIn("\n", spoken)


class Gather(unittest.TestCase):
    def test_slow_or_failing_sources_are_skipped(self):
        import time
        from plugins import daily_briefing as b
        with patch.object(b, "_missions", return_value=([task(1, "A")], None)), \
             patch.object(b, "_canvas", side_effect=lambda: time.sleep(2) or ([], None)):
            tasks, events, canvas = sb.gather(timeout=0.3)
        self.assertEqual([t["title"] for t in tasks], ["A"])
        self.assertIsNone(events)
        self.assertIsNone(canvas)

    def test_slow_missions_are_unknown_not_empty(self):
        import time
        from plugins import daily_briefing as b
        with patch.object(b, "_missions", side_effect=lambda: time.sleep(2) or ([], None)), \
             patch.object(b, "_canvas", return_value=([], "off")):
            self.assertEqual(sb.gather(timeout=0.2), (None, None, None))

    def test_disabled_missions_give_empty(self):
        from plugins import daily_briefing as b
        with patch.object(b, "_missions", return_value=([], "Mission Control is disabled.")), \
             patch.object(b, "_canvas", return_value=([], "Canvas is disabled.")):
            self.assertEqual(sb.gather(timeout=1), (None, None, None))
        self.assertEqual(sb.compose(None, None, None, TODAY), ("", ""))


if __name__ == "__main__":
    unittest.main()
