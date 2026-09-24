import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.install_macos_app import BUNDLE_ID, compatible_arch, install, relocate_project
from unittest.mock import patch


class MacLauncherTests(unittest.TestCase):
    def test_chooses_architecture_that_imports_audio(self):
        with patch("tools.install_macos_app.subprocess.run") as run:
            run.side_effect = [type("Result", (), {"returncode": 0})()]
            self.assertEqual(compatible_arch(Path(sys.executable)), "arm64")
            self.assertEqual(run.call_args.args[0][1], "-arm64")

    def test_relocation_keeps_checkout_and_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "Desktop" / "Jarvis-for-Nathan"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("pass\n")
            (repo / ".git").mkdir()
            (repo / "config").mkdir()
            (repo / "config" / "api_keys.json").write_text("private")
            destination = root / "Projects" / repo.name
            self.assertEqual(relocate_project(repo, destination), destination)
            self.assertEqual((destination / "config" / "api_keys.json").read_text(), "private")
            with self.assertRaisesRegex(ValueError, "already exists"):
                relocate_project(destination, destination)

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

    def test_macos_installs_native_microphone_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").touch()
            target = root / "Jarvis.app"
            with patch("tools.install_macos_app.platform.system", return_value="Darwin"), \
                 patch("tools.install_macos_app.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = ""
                install(root, Path(sys.executable), target, architecture="arm64")
            with (target / "Contents" / "Resources" / "launch.plist").open("rb") as source:
                self.assertEqual(plistlib.load(source)["Arch"], "arm64")
            self.assertEqual(run.call_count, 4)
            self.assertIn("swiftc", run.call_args_list[1].args[0])
            self.assertTrue(any(str(source).endswith("notification_announcer.swift")
                                for source in run.call_args_list[1].args[0]))
            self.assertEqual(run.call_args_list[2].args[0][3], "-")

    def test_unchanged_signed_bundle_does_not_rebuild_or_change_permission_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").touch()
            target = root / "Jarvis.app"
            with patch("tools.install_macos_app.platform.system", return_value="Darwin"), \
                 patch("tools.install_macos_app.subprocess.run") as run:
                run.return_value.returncode = 0
                install(root, Path(sys.executable), target, architecture="arm64",
                        identity="Apple Development: Nathan (ABC123)")
                run.reset_mock()
                install(root, Path(sys.executable), target, architecture="arm64",
                        identity="Apple Development: Nathan (ABC123)")
            self.assertEqual(run.call_count, 1)
            self.assertIn("--verify", run.call_args.args[0])

    def test_failed_compile_keeps_existing_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").touch()
            target = root / "Jarvis.app"
            with patch("tools.install_macos_app.platform.system", return_value="Darwin"), \
                 patch("tools.install_macos_app.subprocess.run") as run:
                run.return_value.returncode = 0
                install(root, Path(sys.executable), target, architecture="arm64", identity="-")
                previous = (target / "Contents" / "Resources" / "launch.plist").read_bytes()
                run.side_effect = [type("Result", (), {"returncode": 1, "stderr": "Swift error"})()]
                with self.assertRaisesRegex(ValueError, "Swift error"):
                    install(root, Path(sys.executable), target, architecture="x86_64", identity="-")
            self.assertEqual((target / "Contents" / "Resources" / "launch.plist").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
