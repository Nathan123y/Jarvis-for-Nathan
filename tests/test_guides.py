"""Game guides: the script, the narration groups, section timing, the cut list and the description."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from worker import guides as g

RAW = {"topic": "money", "title": "GTA 6: The Fastest Ways to Make Money", "thumbnail": "Easy Millions", "description": "How to earn fast.",
       "tags": ["GTA 6 money!", "gta 6 money!", "heists"], "sources": ["https://example.com/a", "not a link"],
       "sections": [{"heading": "Hook", "say": "Money is easy. " * 20, "show": "driving at night"},
                    {"heading": "Heists First", "say": "Do the heists. " * 20, "show": "a shootout"},
                    {"heading": "", "say": "  "},
                    {"heading": "Stocks", "say": "Buy low. " * 30, "show": "the phone"}]}


class ScriptTests(unittest.TestCase):
    def test_script_is_tidied(self):
        s = g.clean_script(RAW, 1)
        self.assertEqual([x["heading"] for x in s["sections"]], ["", "Heists First", "Stocks"])   # hook has no heading; empty dropped
        self.assertEqual(s["tags"], ["gta 6 money", "heists"])
        self.assertEqual(s["sources"], ["https://example.com/a"])

    def test_thin_scripts_are_refused(self):
        with self.assertRaises(ValueError):
            g.clean_script(dict(RAW, sections=RAW["sections"][:2]), 1)
        with self.assertRaises(ValueError):
            g.clean_script(RAW, 7)                              # far too short for seven minutes
        with self.assertRaises(ValueError):
            g.clean_script("nope", 1)

    def test_no_claude_means_try_later(self):
        with self.assertRaises(g.Busy):
            g.write_script("x", dict(g.DEFAULTS), [], ask=lambda prompt, effort="": "")

    def test_narration_is_grouped_into_few_requests(self):
        secs = [{"say": "w " * n} for n in (100, 100, 100, 200, 400, 50)]
        self.assertEqual(g.voice_groups(secs), [[0, 1, 2], [3], [4], [5]])

    def test_sections_start_where_their_words_are_heard(self):
        secs = [{"say": "a b c d"}, {"say": "e f g h i j"}, {"say": "k l"}]
        words = [(i * 1.0, i * 1.0 + .5, "w", .9) for i in range(12)]
        self.assertEqual(g.section_times(secs, words, 12.0), [0.0, 3.85, 9.85])
        self.assertEqual(g.section_times(secs, [], 12.0), [0.0, 4.0, 10.0])


class FootageTests(unittest.TestCase):
    def test_spans_are_cleaned(self):
        raw = {"is_game": True, "clean": True, "spans": [{"section": 1, "start": 10, "end": 30}, {"section": 9, "start": 40, "end": 140},
                                                         {"section": 0, "start": 20, "end": 35}, {"section": 0, "start": 500, "end": 503}, "x"]}
        self.assertEqual(g.clean_spans(raw, 600, 3), [{"section": 1, "start": 10, "end": 30}, {"section": -1, "start": 40, "end": 80}])
        self.assertEqual(g.clean_spans(dict(raw, clean=False), 600, 3), [])
        self.assertEqual(g.clean_spans(dict(raw, is_game=False), 600, 3), [])

    def test_cut_list_fills_every_section_exactly(self):
        pieces = [{"path": "a.mp4", "section": 0, "seconds": 13}, {"path": "b.mp4", "section": 1, "seconds": 7},
                  {"path": "c.mp4", "section": -1, "seconds": 20}]
        starts, total = [0.0, 10.0], 40.0
        shots = g.plan_shots(pieces, starts, total)
        self.assertAlmostEqual(sum(s["seconds"] for s in shots), total, places=1)
        self.assertEqual((shots[0]["path"], shots[0]["at"]), ("a.mp4", 0.0))        # a section's own footage first
        first_b = next(i for i, s in enumerate(shots) if s["path"] == "b.mp4")
        self.assertGreaterEqual(sum(s["seconds"] for s in shots[:first_b]), 10.0 - .01)   # section 1's footage only in section 1
        self.assertTrue(all(s["seconds"] >= 1.5 for s in shots))
        self.assertNotEqual(shots[0]["zoom_in"], shots[1]["zoom_in"])

    def test_shot_command_zooms_and_handles_silent_footage(self):
        cmd = g.shot_command({"path": "a.mp4", "at": 6.0, "seconds": 6.0, "zoom_in": True}, Path("o.mp4"), has_audio=False)
        self.assertIn("anullsrc=r=48000:cl=stereo", cmd)
        self.assertIn("1+0.05*t/6.00", cmd[cmd.index("-vf") + 1])
        self.assertIn("1:a:0", cmd)


class DescriptionTests(unittest.TestCase):
    def test_chapters_and_credits(self):
        s = g.clean_script(RAW, 1)
        text = g.describe(s, [0.0, 14.2, 71.0], [{"channel": "Some Gamer", "url": "https://youtu.be/x"}])
        self.assertIn("0:00 Intro\n0:14 Heists First\n1:11 Stocks", text)
        self.assertIn("Gameplay footage from:\nSome Gamer - https://youtu.be/x", text)
        self.assertEqual(g.chapters(s["sections"], [0.0, 4.0, 8.0]), "")          # too close together for YouTube

    def test_thumbnail_is_drawn(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            frame = Path(d) / "f.png"
            Image.new("RGB", (1920, 1080), (40, 90, 160)).save(frame)
            out = g.thumbnail(frame, "Easy Millions", Path(d) / "t.jpg")
            self.assertEqual(Image.open(out).size, (1280, 720))


class SlotTests(unittest.TestCase):
    def test_nothing_before_launch_day_and_one_a_day_after(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(g.DEFAULTS, start_date="2026-11-19", time="16:00")
            rec = {"path": "x.mp4", "title": "t", "seconds": 400, "made": 1.0}
            with patch.object(g, "STATE", Path(d) / "guides.json"), patch.object(g, "settings", lambda: cfg), \
                    patch.object(g, "make", return_value=rec) as make, patch.object(g.GuideService, "deliver", return_value="posted"):
                svc = g.GuideService(log=lambda m: None)
                self.assertEqual(svc.run_once(datetime(2026, 11, 18, 16, 5)), "")
                self.assertEqual(svc.run_once(datetime(2026, 11, 19, 15, 0)), "")
                self.assertEqual(svc.run_once(datetime(2026, 11, 19, 16, 5)), "posted")
                self.assertEqual(svc.run_once(datetime(2026, 11, 19, 17, 0)), "")
                self.assertEqual(make.call_count, 1)


if __name__ == "__main__":
    unittest.main()
