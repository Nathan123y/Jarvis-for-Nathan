"""Claude leads the reasoning ladder only, and a test run never reaches the real CLI."""
import unittest
from unittest import mock

from core import gemini


class ClaudeRungTests(unittest.TestCase):
    def test_claude_leads_smart_and_stays_off_fast_and_search(self):
        self.assertEqual(gemini._LADDERS[gemini.SMART][0], gemini.CLAUDE)
        self.assertNotIn(gemini.CLAUDE, gemini._LADDERS[gemini.FAST])
        self.assertNotIn(gemini.CLAUDE, gemini._LADDERS[gemini.SEARCH])

    def test_tests_never_run_the_cli(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(gemini.claude_text("hi"), "")
        run.assert_not_called()

    def test_a_failed_claude_turn_falls_through_to_gemini(self):
        with mock.patch.object(gemini, "_live_call", return_value=gemini._Reply("from gemini")), \
             mock.patch.object(gemini, "api_key", return_value="k"):
            self.assertEqual(gemini.text("hi", tier=gemini.SMART), "from gemini")


if __name__ == "__main__":
    unittest.main()
