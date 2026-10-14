"""read / open / info find a file by name; close matches the window title."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import file_controller as fc


class TargetTests(unittest.TestCase):
    def test_existing_path_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.txt"
            f.write_text("x")
            self.assertEqual(fc._target(str(f)), (f, ""))

    def test_a_guessed_path_falls_back_to_the_single_good_match(self):
        real = Path("/home/u/Downloads/EE98 - FL26 - Homework 1.pdf")
        with patch.object(fc, "_scored_matches", return_value=[(0.95, real), (0.4, Path("/x"))]):
            self.assertEqual(fc._target("desktop", "EE98 homework 1"), (real, ""))

    def test_two_equally_good_matches_ask_which(self):
        a, b = Path("/h/a/Notes.txt"), Path("/h/b/Notes.txt")
        with patch.object(fc, "_scored_matches", return_value=[(0.95, a), (0.95, b)]):
            _, note = fc._target("desktop", "Notes.txt")
        self.assertIn(str(a), note)
        self.assertIn(str(b), note)


class CloseTests(unittest.TestCase):
    def run_close(self, stdout="", returncode=0, stderr=""):
        result = type("R", (), {"stdout": stdout, "returncode": returncode, "stderr": stderr})()
        with patch.object(fc, "_OS", "Darwin"), \
             patch.object(fc.subprocess, "run", return_value=result) as run:
            return fc.close_file(name="Report.pdf"), run

    def test_matches_name_then_stem(self):
        message, run = self.run_close(stdout="Preview\n")
        self.assertEqual(run.call_args.args[0][-2:], ["Report.pdf", "Report"])
        self.assertIn("Closed Report.pdf in Preview", message)

    def test_nothing_open(self):
        self.assertIn("No open window", self.run_close()[0])

    def test_missing_accessibility_permission_is_explained(self):
        message, _ = self.run_close(returncode=1, stderr="osascript is not allowed assistive access")
        self.assertIn("Accessibility", message)


if __name__ == "__main__":
    unittest.main()
