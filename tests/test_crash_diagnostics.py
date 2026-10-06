import json
import tempfile
import unittest
from pathlib import Path

from core import crashlog
from tools import last_crash


class CrashTools(unittest.TestCase):
    def test_summarise_ips(self):
        head = json.dumps({"app_name": "Python", "timestamp": "2026-10-05 18:01:00"})
        body = json.dumps({
            "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"},
            "faultingThread": 1,
            "usedImages": [{"name": "QtGui"}, {"name": "libsystem"}],
            "threads": [{"frames": []},
                        {"queue": "com.apple.main-thread",
                         "frames": [{"imageIndex": 0, "symbol": "QPainter::drawPixmap"},
                                    {"imageIndex": 7, "symbol": "x"}]}]})
        out = last_crash.summarise_ips(head + "\n" + body)
        self.assertIn("EXC_BAD_ACCESS", out[1])
        self.assertIn("com.apple.main-thread", out[2])
        self.assertIn("QPainter::drawPixmap", out[3])
        self.assertIn("?", out[4])

    def test_bad_report(self):
        self.assertEqual(last_crash.summarise_ips("nonsense"), ["(could not read this crash report)"])

    def test_latest_report_picks_newest_python(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "Python-old.ips").write_text("a")
            (d / "Other.ips").write_text("b")
            self.assertEqual(last_crash.latest_report(d).name, "Python-old.ips")
            self.assertIsNone(last_crash.latest_report(d / "missing"))

    def test_python_stack_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "crash.log"
            p.write_text("=== start ===\nFatal Python error: old\n\nFatal Python error: Segmentation fault\n\nThread 0x1:\n  File \"ui.py\", line 9 in paintEvent\n")
            lines = last_crash.last_python_stack(p)
            self.assertEqual(lines[0], "Fatal Python error: Segmentation fault")
            self.assertIn("paintEvent", lines[-1])
            self.assertEqual(last_crash.last_python_stack(Path(d) / "none"), [])

    def test_enable_writes_marker(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "crash.log"
            old = crashlog._handle
            crashlog._handle = None
            try:
                self.assertEqual(crashlog.enable(p), p)
                self.assertIn("Jarvis started", p.read_text())
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            finally:
                import faulthandler
                faulthandler.disable()
                if crashlog._handle:
                    crashlog._handle.close()
                crashlog._handle = old


if __name__ == "__main__":
    unittest.main()
