import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.install_macos_app import BUNDLE_ID, install


class MacLauncherTests(unittest.TestCase):
    def test_installed_app_runs_existing_checkout_and_reinstall_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "Jarvis project"
            repo.mkdir()
            (repo / "main.py").write_text("print('launch from repo')\n")
            target = root / "Applications" / "Jarvis.app"
            install(repo, Path(sys.executable), target)
            with (target / "Contents" / "Info.plist").open("rb") as source:
                self.assertEqual(plistlib.load(source)["CFBundleIdentifier"], BUNDLE_ID)
            env = dict(os.environ, HOME=str(root))
            result = subprocess.run([str(target / "Contents" / "MacOS" / "Jarvis")],
                                    env=env, timeout=5, check=False)
            self.assertEqual(result.returncode, 0)
            log = root / "Library" / "Logs" / "Jarvis" / "launch.log"
            self.assertIn("launch from repo", log.read_text())
            self.assertEqual(install(repo, Path(sys.executable), target), target)

    def test_unrelated_app_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").touch()
            target = root / "Jarvis.app"
            target.mkdir()
            with self.assertRaisesRegex(ValueError, "unrelated app"):
                install(root, Path(sys.executable), target)


if __name__ == "__main__":
    unittest.main()
