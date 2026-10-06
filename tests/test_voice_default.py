import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import config_manager as cm


class VoiceDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for name, val in (("CONFIG_DIR", d), ("CONFIG_FILE", d / "api_keys.json")):
            p = patch.object(cm, name, val)
            p.start()
            self.addCleanup(p.stop)

    def write(self, data):
        cm.CONFIG_FILE.write_text(json.dumps(data))

    def test_default_is_a_male_voice(self):
        self.assertEqual(cm.get_voice(), "Algenib")

    def test_saved_gacrux_from_old_default_moves_once(self):
        self.write({"voice_name": "Gacrux", "gemini_api_key": "x"})
        self.assertEqual(cm.get_voice(), "Algenib")
        data = json.loads(cm.CONFIG_FILE.read_text())
        self.assertEqual(data["voice_name"], "Algenib")
        self.assertTrue(data["voice_default_v2"])
        self.assertEqual(data["gemini_api_key"], "x")     # rest of config untouched

    def test_deliberate_choice_sticks(self):
        cm.save_voice("Gacrux")
        self.assertEqual(cm.get_voice(), "Gacrux")
        cm.save_voice("Charon")
        self.assertEqual(cm.get_voice(), "Charon")

    def test_unknown_voice_falls_back(self):
        self.write({"voice_name": "Nope"})
        self.assertEqual(cm.get_voice(), "Algenib")


if __name__ == "__main__":
    unittest.main()
