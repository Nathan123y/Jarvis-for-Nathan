"""Guards that stop model output from deleting or clobbering the user's files."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import code_helper, desktop, reminder


class DesktopSandboxTests(unittest.TestCase):
    def test_plain_code_runs(self):
        self.assertEqual(desktop._execute_generated_code("print(1 + 1)"), "2")

    def test_deletion_and_escapes_are_refused(self):
        for code in ('Path("x").unlink()',
                     'Path("d").rmdir()',
                     'getattr(Path, "unlink")(Path("x"))',
                     '().__class__.__bases__',
                     'import os'):
            with self.subTest(code=code):
                self.assertTrue(desktop._unsafe_reason(code))
                self.assertIn("refused", desktop._execute_generated_code(code))


class CodeSaveTests(unittest.TestCase):
    def test_overwrite_keeps_backup(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "app.py"
            f.write_text("original\n", encoding="utf-8")
            code_helper._save_file(f, "new\n")
            self.assertEqual(f.read_text(encoding="utf-8"), "new\n")
            self.assertEqual((Path(d) / "app.py.bak").read_text(encoding="utf-8"), "original\n")

    def test_empty_output_leaves_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "app.py"
            f.write_text("original\n", encoding="utf-8")
            self.assertIn("Not saved", code_helper._save_file(f, "  \n"))
            self.assertEqual(f.read_text(encoding="utf-8"), "original\n")


class MacReminderCleanupTests(unittest.TestCase):
    def test_script_unloads_its_own_job_last(self):
        block = reminder._mac_cleanup_block("JARVISReminder_X")
        self.assertIn("com.jarvis.reminder.JARVISReminder_X.plist", block)
        self.assertLess(block.index("unlink"), block.index('"launchctl", "remove"'))


if __name__ == "__main__":
    unittest.main()
