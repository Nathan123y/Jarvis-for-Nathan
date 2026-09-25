import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import reset_macos_access as repair


class ResetMacAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "Jarvis.app"
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / "Contents" / "MacOS" / "Jarvis").touch()
        with (self.app / "Contents" / "Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleIdentifier": repair.BUNDLE_ID}, file)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reset_targets_only_requested_services(self):
        def respond(argv, **kwargs):
            if argv[0] == "/bin/ps":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(repair.platform, "system", return_value="Darwin"), \
             patch.object(repair.subprocess, "run", side_effect=respond) as run:
            result = repair.reset("all", self.app)
        services = [call.args[0][2] for call in run.call_args_list
                    if call.args[0][0] == "/usr/bin/tccutil"]
        self.assertEqual(services, list(repair.SERVICES["all"]))
        self.assertNotIn("ScreenCapture", services)
        self.assertNotIn("Microphone", services)
        self.assertEqual(len(result), len(services))

    def test_refuses_unknown_app_without_resetting_anything(self):
        with (self.app / "Contents" / "Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleIdentifier": "unrelated.app"}, file)
        with patch.object(repair.platform, "system", return_value="Darwin"), \
             patch.object(repair.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not the Jarvis launcher"):
                repair.reset("data", self.app)
        run.assert_not_called()

    def test_requires_jarvis_to_be_quit_first(self):
        def respond(argv, **kwargs):
            output = str(self.app / "Contents" / "MacOS" / "Jarvis") if argv[0] == "/bin/ps" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        with patch.object(repair.platform, "system", return_value="Darwin"), \
             patch.object(repair.subprocess, "run", side_effect=respond) as run:
            with self.assertRaisesRegex(ValueError, "Quit Jarvis"):
                repair.reset("controls", self.app)
        self.assertFalse(any(call.args[0][0] == "/usr/bin/tccutil" for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
