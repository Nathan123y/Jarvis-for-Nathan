"""Closing the user's own browser tabs by name. AppleScript is faked."""
import unittest
from unittest.mock import patch

from actions import browser_control as bc
from core import undo

TABS = [
    (1, 1, "Inbox (3) - Gmail", "https://mail.google.com/mail/u/0/"),
    (1, 2, "Lofi beats - YouTube", "https://www.youtube.com/watch?v=abc"),
    (2, 1, "Jarvis-for-Nathan · GitHub", "https://github.com/nathan/jarvis"),
    (2, 2, "Drake - YouTube", "https://www.youtube.com/watch?v=def"),
]


class CloseTabTests(unittest.TestCase):
    def setUp(self):
        self.scripts = []
        self.reopened = []
        for p in (patch.object(bc, "_OS", "Darwin"),
                  patch.object(bc, "_detect_default_browser", return_value="chrome"),
                  patch.object(bc, "_list_tabs", side_effect=lambda app: list(self.tabs)),
                  patch.object(bc, "_osascript", side_effect=self.osa),
                  patch.object(bc.subprocess, "run",
                               side_effect=lambda cmd, **kw: self.reopened.append(cmd[-1]))):
            p.start()
            self.addCleanup(p.stop)
        self.tabs = TABS
        undo.clear()
        self.addCleanup(undo.clear)

    def osa(self, script, timeout=8):
        self.scripts.append(script)
        return "https://front.example/\n"

    def run_tab(self, text):
        return bc.browser_control({"action": "close_tab", "text": text})

    def test_closes_matching_tabs_highest_index_first(self):
        result = self.run_tab("youtube")
        self.assertEqual(result, "Closed 2 tabs: Lofi beats - YouTube; Drake - YouTube.")
        self.assertEqual(self.scripts[-1].split("\n")[1:3],
                         ["  close tab 2 of window 2", "  close tab 2 of window 1"])

    def test_matches_title_words_site_and_split_names(self):
        self.assertEqual(self.run_tab("the gmail tab"), "Closed Inbox (3) - Gmail.")
        self.assertIn("GitHub", self.run_tab("git hub"))
        self.assertIn("Drake", self.run_tab("drake youtube"))

    def test_no_match_lists_open_tabs_and_closes_nothing(self):
        result = self.run_tab("netflix")
        self.assertIn("No open tab matches 'netflix'", result)
        self.assertIn("Gmail", result)
        self.assertEqual(self.scripts, [])

    def test_too_many_matches_asks_which_one(self):
        self.tabs = [(1, i, f"Doc {i} - Google Docs", f"https://docs.google.com/{i}") for i in range(1, 6)]
        self.assertIn("5 tabs match", self.run_tab("google docs"))
        self.assertEqual(self.scripts, [])

    def test_no_name_closes_the_front_tab(self):
        self.assertEqual(bc.browser_control({"action": "close_tab"}), "Closed the current tab.")
        self.assertIn("close active tab of front window", self.scripts[-1])

    def test_undo_reopens_what_was_closed(self):
        self.run_tab("drake")
        self.assertIn("Reopened 1 tab", undo.undo_last())
        self.assertEqual(self.reopened, ["https://www.youtube.com/watch?v=def"])

    def test_browser_closed_or_permission_missing(self):
        with patch.object(bc, "_list_tabs", return_value=None):
            self.assertEqual(self.run_tab("gmail"), "Google Chrome isn't open.")
        with patch.object(bc, "_list_tabs", side_effect=PermissionError("not authorized")):
            self.assertIn("permission to control Google Chrome", self.run_tab("gmail"))


if __name__ == "__main__":
    unittest.main()
