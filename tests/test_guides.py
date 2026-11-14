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
    SHOTS = [{"id": 0, "source": "a", "start": 0, "end": 10, "what": "city"}, {"id": 1, "source": "a", "start": 10, "end": 20, "what": "car chase"},
             {"id": 2, "source": "b", "start": 0, "end": 8, "what": "beach"}, {"id": 3, "source": "b", "start": 8, "end": 30, "what": "gym"}]

    def test_catalogue_shots_are_cleaned(self):
        raw = {"shots": [{"start": 0, "end": 10, "what": "A car  drives"}, {"start": 5, "end": 12, "what": "overlap"},
                         {"start": 20, "end": 21, "what": "too short"}, {"start": 30, "end": 100, "what": "long one"},
                         {"start": 590, "end": 640, "what": "past the end"}, {"start": 40, "end": 50, "what": ""}, "x",
                         {"start": 80, "end": 85, "what": "Rockstar Games logo on a black background"}]}
        self.assertEqual(g.clean_shots(raw, 600), [{"start": 0, "end": 10, "what": "A car drives"}, {"start": 30, "end": 70, "what": "long one"}])
        self.assertEqual(g.clean_shots(None, 600), [])

    def test_library_is_one_flat_catalogue(self):
        lib = {"sources": {"a": {"shots": [{"start": 0, "end": 5, "what": "x"}]}, "b": {"shots": [{"start": 2, "end": 9, "what": "y"}]}}}
        self.assertEqual([(c["id"], c["source"]) for c in g.catalogue(lib)], [(0, "a"), (1, "b")])

    def test_picks_use_each_shot_once_and_short_sections_are_topped_up(self):
        got = g.clean_match({"sections": [[1, "1", 99, "x"], [1, 3]]}, [15, 10, 5], self.SHOTS)
        self.assertEqual(got, [[1, 0], [3], [2]])                 # 1 once; section 0 topped up; section 2 gets what's left
        self.assertEqual(g.clean_match({}, [12, 5], self.SHOTS), [[0, 1], [2]])
        self.assertEqual(g.fallback_match([12, 5], self.SHOTS), [[0, 1], [2]])

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


class ShortTests(unittest.TestCase):
    RAW = {"title": "5 Crazy Things In GTA 6", "headline": "5 crazy things in GTA 6", "hook": "Here's five crazy things you can do in GTA 6.",
           "items": [{"headline": "Six stars", "say": "Number %d. " % n + "The cops remember you now. " * 5, "show": "police chase"} for n in range(1, 6)],
           "outro": "Which one first? And in case you missed it...", "caption": "which one?", "hashtags": ["#GTA6", "six stars"],
           "sources": ["https://example.com"]}

    def test_short_script_becomes_numbered_sections(self):
        s = g.clean_short(self.RAW)
        self.assertEqual([x["heading"] for x in s["sections"]][:3], ["5 CRAZY THINGS IN GTA 6", "1. SIX STARS", "2. SIX STARS"])
        self.assertEqual(s["sections"][-1]["heading"], "")
        self.assertEqual(s["hashtags"][:3], ["gta6", "sixstars", "gtavi"])
        with self.assertRaises(ValueError):
            g.clean_short(dict(self.RAW, items=self.RAW["items"][:2]))
        with self.assertRaises(ValueError):
            g.clean_short(dict(self.RAW, items=[dict(i, say="word " * 80) for i in self.RAW["items"]]))

    def test_short_text_layer(self):
        with tempfile.TemporaryDirectory() as d:
            text = g.write_ass_short([(0.5, 1.2, "HERE'S FIVE")], [(0, 4, "5 CRAZY THINGS"), (4, 9, "1. SIX {STARS}"), (9, 12, "")], Path(d) / "s.ass").read_text()
            self.assertIn("PlayResY: 1920", text)
            self.assertIn("{\\c&H0021DEFF&}1. {\\c&H00FFFFFF&}SIX STARS", text)
            self.assertEqual(text.count("Dialogue: 1,"), 2)
        cmd = g.shot_command({"path": "a.mp4", "at": 0, "seconds": 3.0, "zoom_in": False}, Path("o.mp4"), True, g.SIZES["short"])
        self.assertIn("crop=1080:1920", cmd[cmd.index("-vf") + 1])

    def test_before_launch_the_writer_is_told_only_official_material_exists(self):
        cfg = dict(g.DEFAULTS, launch_date="2026-11-19")
        self.assertIn("NOT out yet", g.when_line(cfg, "2026-11-13"))
        self.assertIn("released on 2026-11-19", g.when_line(cfg, "2026-11-19"))


class SlotTests(unittest.TestCase):
    def test_nothing_before_the_start_then_each_kind_at_its_times(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(g.DEFAULTS, start_date="2026-11-13", time="16:00", short_times="12:00,19:30")
            made = []

            def fake(topic, cfg, log, kind="guide"):
                made.append(kind)
                return {"kind": kind, "path": f"{len(made)}.mp4", "title": "t", "seconds": 60, "made": 1.0}
            with patch.object(g, "STATE", Path(d) / "guides.json"), patch.object(g, "settings", lambda: cfg), \
                    patch.object(g, "make", side_effect=fake), patch.object(g.GuideService, "deliver", return_value="posted"):
                svc = g.GuideService(log=lambda m: None)
                self.assertEqual(svc.run_once(datetime(2026, 11, 12, 16, 5)), "")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 11, 0)), "")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 12, 5)), "posted")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 12, 9)), "")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 16, 5)), "posted")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 19, 31)), "posted")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 19, 40)), "")
                self.assertEqual(made, ["short", "guide", "short"])

    def test_a_busy_day_waits_then_gives_the_slot_up(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(g.DEFAULTS, start_date="2026-11-13", time="16:00", short_times="")
            with patch.object(g, "STATE", Path(d) / "guides.json"), patch.object(g, "settings", lambda: cfg), \
                    patch.object(g, "make", side_effect=g.Busy("no voice")):
                svc = g.GuideService(log=lambda m: None)
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 16, 5)), "waiting")
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 16, 7)), "")          # backing off
                st = g.wv.load_state(g.STATE); st["busy_until"] = 0; g.wv.save_state(st, g.STATE)
                self.assertEqual(svc.run_once(datetime(2026, 11, 13, 16, 40)), "waiting")
                self.assertEqual(g.wv.load_state(g.STATE)["slots"], {"2026-11-13": ["16:00"]})   # given up for today


if __name__ == "__main__":
    unittest.main()
