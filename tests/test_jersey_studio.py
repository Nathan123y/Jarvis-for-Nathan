"""ChatGPT jersey art: prompts carry no real teams, the art lands whole in the inbox, failures are counted."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from worker import jersey_studio as js

REAL = ["barcelona", "lakers", "madrid", "raiders", "dolphins", "kobe", "ronaldinho", "edwards", "lamar", "bulls",
        "celtics", "warriors", "heat", "messi", "jordan", "timberwolves", "ravens", "mamba", "rose city"]


class StudioTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_no_real_team_or_player_in_any_prompt(self):
        for c in js.CONCEPTS:
            text = (js.prompt(c) + " " + c[6]).lower()
            for word in REAL:
                self.assertNotIn(word, text.replace("no real team", ""), f"{c[0]} mentions {word}")

    def test_soccer_asks_for_sleeves(self):
        self.assertIn("sleeve.png", js.prompt(js.concept("sunset-tide")))
        self.assertIn("sleeve.png", js.prompt(js.concept("starfire")))       # every new jersey is soccer now
        self.assertNotIn("basketball", js.prompt(js.concept("gingerbread")).split("THE STYLE")[0])

    def fake_codex(self, names, size=(1024, 1536)):
        def run(argv, **kw):
            for n in names:
                Image.new("RGB", size, (10, 20, 60)).save(Path(kw["cwd"]) / n)
            return mock.Mock(returncode=0, stdout="", stderr="")
        return run

    def test_generate_puts_the_art_in_the_inbox(self):
        with mock.patch.object(js, "codex_bin", return_value="codex"):
            out = js.generate("sunset-tide", inbox=self.tmp / "inbox",
                              run=self.fake_codex(["front.png", "back.png", "sleeve.png"]))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["back.png", "front.png", "info.txt", "sleeve.png"])
        self.assertIn("soccer", (out / "info.txt").read_text())

    def test_missing_art_fails_and_leaves_nothing(self):
        with mock.patch.object(js, "codex_bin", return_value="codex"):
            with self.assertRaises(RuntimeError):
                js.generate("starfire", inbox=self.tmp / "inbox", run=self.fake_codex(["front.png", "back.png"]))
        self.assertFalse((self.tmp / "inbox" / "starfire").exists())

    def test_next_concepts_skips_made_and_twice_failed(self):
        path = self.tmp / "s.json"
        js.save({"made": {"starfire": 1}, "failed": {"garnet-royale": 2}}, path)
        self.assertEqual(js.next_concepts(1, path), [js.CONCEPTS[0][0]])
        js.save({"made": {"starfire": 1, "north-pole": 1}, "failed": {"candy-cane": 2}}, path)
        self.assertEqual(js.next_concepts(1, path), ["frostbite"])


if __name__ == "__main__":
    unittest.main()
