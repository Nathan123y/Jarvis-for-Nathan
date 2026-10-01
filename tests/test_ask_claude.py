import unittest
from unittest.mock import patch

from actions import ask_claude


class AskClaudeTests(unittest.TestCase):
    def test_mac_handoff_copies_question_and_opens_app(self):
        with patch.object(ask_claude.platform, "system", return_value="Darwin"), \
             patch.object(ask_claude.subprocess, "run") as run, \
             patch.object(ask_claude.webbrowser, "open") as browser:
            result = ask_claude.ask_claude({"question": "  Explain this circuit  "})
        self.assertEqual(run.call_args_list[0].args, (["pbcopy"],))
        self.assertEqual(run.call_args_list[0].kwargs["input"], "Explain this circuit")
        self.assertEqual(run.call_args_list[1].args, (["open", "-a", "Claude"],))
        browser.assert_not_called()
        self.assertIn("Paste and send", result)
        self.assertIn("cannot read Claude's reply", result)


if __name__ == "__main__":
    unittest.main()
