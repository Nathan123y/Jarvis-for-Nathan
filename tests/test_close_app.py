import unittest
from unittest.mock import patch, MagicMock

from actions import computer_settings as cs


class CloseApp(unittest.TestCase):
    def test_detect_close_by_name(self):
        self.assertEqual(cs._detect_action("close Spotify"), {"action": "close_app", "value": "spotify"})
        self.assertEqual(cs._detect_action("quit the Safari app")["value"], "safari")
        self.assertNotEqual(cs._detect_action("close this window").get("value"), "this window")
        self.assertNotEqual(cs._detect_action("close tab")["action"], "close_app")

    def test_match_app(self):
        running = ["Finder", "Spotify", "Google Chrome", "Messages"]
        self.assertEqual(cs._match_app("spotify", running), "Spotify")
        self.assertEqual(cs._match_app("chrome", running), "Google Chrome")
        self.assertEqual(cs._match_app("spotfy", running), "Spotify")
        self.assertEqual(cs._match_app("zoom", running), "")

    def test_never_closes_jarvis_by_name(self):
        for name in ("Jarvis", "python", "Python3.13"):
            with patch.object(cs, "subprocess") as sp:
                self.assertIn("Jarvis itself", cs.close_named_app(name))
                sp.run.assert_not_called()

    def test_no_name_and_jarvis_in_front_does_nothing(self):
        with patch.object(cs, "_frontmost_is_jarvis", return_value=True), \
             patch.object(cs, "close_app") as hot:
            self.assertIn("which app", cs.close_named_app(""))
            hot.assert_not_called()

    def test_quits_named_app_on_mac(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            out = MagicMock(returncode=0, stderr="")
            out.stdout = "Finder, Python, Spotify" if "background only" in cmd[-1] else ""
            return out
        with patch.object(cs, "_OS", "Darwin"), patch.object(cs.subprocess, "run", side_effect=run):
            self.assertEqual(cs.close_named_app("spotify"), "Closed Spotify.")
        self.assertEqual(calls[-1], ["osascript", "-e", 'tell application "Spotify" to quit'])

    def test_python_never_a_candidate(self):
        def run(cmd, **kw):
            return MagicMock(returncode=0, stderr="", stdout="Finder, Python")
        with patch.object(cs, "_OS", "Darwin"), patch.object(cs.subprocess, "run", side_effect=run):
            self.assertIn("isn't open", cs.close_named_app("pyth"))

    def test_close_window_guard(self):
        with patch.object(cs, "_frontmost_is_jarvis", return_value=True), \
             patch.object(cs, "_PYAUTOGUI", True):
            out = cs.computer_settings({"action": "close_window"})
        self.assertIn("didn't close", out)

    def test_dispatch_uses_name(self):
        with patch.object(cs, "close_named_app", return_value="Closed Spotify.") as c, \
             patch.object(cs, "_PYAUTOGUI", True):
            self.assertEqual(cs.computer_settings({"action": "close_app", "value": "Spotify"}), "Closed Spotify.")
            c.assert_called_once_with("Spotify")


if __name__ == "__main__":
    unittest.main()
