"""Saving one setting never erases the others."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import perf_trace
from memory import config_manager as c


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "api_keys.json"
        patcher = patch.multiple(c, CONFIG_DIR=self.dir, CONFIG_FILE=self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unreadable_file_is_left_alone(self):
        self.file.write_text('{"gemini_api_key": "KEEP"', encoding="utf-8")
        c.save_voice("Charon")
        c._save_flag("proactive_audio", False)
        c.save_turn_tuning({"enabled": True})
        self.assertEqual(self.file.read_text(encoding="utf-8"), '{"gemini_api_key": "KEEP"')

    def test_a_save_keeps_other_keys_and_leaves_no_temp_file(self):
        self.file.write_text(json.dumps({"gemini_api_key": "KEEP"}), encoding="utf-8")
        c.save_turn_tuning({"enabled": True, "silence_ms": 500})
        data = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(data["gemini_api_key"], "KEEP")
        self.assertEqual(data["turn_tuning"]["silence_ms"], 500)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["api_keys.json"])


class ReplyDelayTraceTests(unittest.TestCase):
    def test_reply_delay_is_recorded_without_content(self):
        with patch.object(perf_trace, "_write") as write:
            perf_trace.log_reply_delay(0.8125, tool=False)
        self.assertEqual(write.call_args.args[0], {"kind": "reply", "ms": 812.5, "tool": False})


if __name__ == "__main__":
    unittest.main()
