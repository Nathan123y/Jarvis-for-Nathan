import subprocess
import unittest
from unittest.mock import patch

from actions import mac_access
from actions import computer_settings as settings
from actions import file_controller as files
from plugins import daily_briefing


class MacAccessTests(unittest.TestCase):
    def test_explicit_contact_check_reports_denial_without_leaking_data(self):
        with patch.object(mac_access.platform, "system", return_value="Darwin"), \
             patch.object(mac_access.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "", "Not authorized to send Apple events (-1743)")):
            result = mac_access.mac_access({"scope": "contacts"})
        self.assertIn("Automation denied", result)
        self.assertIn("Contacts", result)
        self.assertNotIn("Desktop", result)

    def test_mac_folder_permission_hint_is_specific(self):
        with patch.object(files, "_OS", "Darwin"):
            result = files._permission_denied(str(files.Path.home() / "Documents" / "note.txt"))
        self.assertIn("Files & Folders", result)
        self.assertIn("Documents", result)

    def test_native_volume_does_not_require_pyautogui_and_reports_failure(self):
        with patch.object(settings, "_OS", "Darwin"), \
             patch.object(settings, "_PYAUTOGUI", False), \
             patch.object(settings, "volume_get", return_value=50), \
             patch.object(settings.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "", "osascript: permission denied (-1743)")):
            result = settings.computer_settings({"action": "volume_set", "value": "40"})
        self.assertIn("Automation", result)
        self.assertNotIn("Volume set", result)

    def test_unmute_uses_unmute_apple_script(self):
        with patch.object(settings, "_OS", "Darwin"), \
             patch.object(settings, "_mac_script") as script:
            settings.ACTION_MAP["unmute"]()
        script.assert_called_once_with("set volume without output muted")

    def test_calendar_distinguishes_automation_denial(self):
        with patch.object(daily_briefing.platform, "system", return_value="Darwin"), \
             patch.object(daily_briefing.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "", "not authorized to send Apple events (-1743)")):
            events, notice = daily_briefing._calendar()
        self.assertEqual(events, [])
        self.assertIn("Automation was denied", notice)


if __name__ == "__main__":
    unittest.main()
