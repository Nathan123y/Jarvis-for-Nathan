import subprocess
import unittest
from unittest.mock import patch

from actions import ask_chatgpt


class AskChatGPTTests(unittest.TestCase):
    def test_mac_opens_app_after_copying_question(self):
        with patch.object(ask_chatgpt.platform, "system", return_value="Darwin"), \
             patch.object(ask_chatgpt.subprocess, "run") as run, \
             patch.object(ask_chatgpt.webbrowser, "open") as browser:
            result = ask_chatgpt.ask_chatgpt({"question": "  Explain the workout data  "})
        self.assertEqual(run.call_args_list[0].args, (["pbcopy"],))
        self.assertEqual(run.call_args_list[0].kwargs["input"], "Explain the workout data")
        self.assertEqual(run.call_args_list[1].args, (["open", "-a", "ChatGPT"],))
        browser.assert_not_called()
        self.assertIn("Paste and send", result)
        self.assertIn("cannot read its reply", result)

    def test_falls_back_to_browser_when_app_unavailable(self):
        with patch.object(ask_chatgpt.platform, "system", return_value="Darwin"), \
             patch.object(ask_chatgpt.subprocess, "run", side_effect=[None, subprocess.CalledProcessError(1, "open")]), \
             patch.object(ask_chatgpt.webbrowser, "open", return_value=True) as browser:
            result = ask_chatgpt.ask_chatgpt({"question": "Hello"})
        browser.assert_called_once_with("https://chatgpt.com/")
        self.assertIn("Paste and send", result)

    def test_clipboard_failure_does_not_open_chatgpt(self):
        with patch.object(ask_chatgpt.platform, "system", return_value="Darwin"), \
             patch.object(ask_chatgpt.subprocess, "run", side_effect=OSError("clipboard unavailable")), \
             patch.object(ask_chatgpt.webbrowser, "open") as browser:
            result = ask_chatgpt.ask_chatgpt({"question": "Hello"})
        browser.assert_not_called()
        self.assertIn("could not copy", result)


if __name__ == "__main__":
    unittest.main()
