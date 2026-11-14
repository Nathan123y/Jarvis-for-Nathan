"""Game clips: the inbox, the cut plan, the ffmpeg call and the daily slots."""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from worker import clips

CFG = dict(clips.DEFAULTS)
RAW = {"ok": True, "start": 12, "end": 40, "title": "He landed a JET on the yacht 🚀 #gta", "highlight": ["jet", "the yacht"],
       "hook": "He has  never flown before.", "caption": "would you bail?", "hashtags": ["#Jet", "GTA6", "yacht party"]}
INFO = {"duration": 60.0, "w": 1920, "h": 1080, "audio": True}


class PlanTests(unittest.TestCase):
    def test_plan_is_tidied(self):
        p = clips.clean_plan(RAW, 60, CFG)
        self.assertEqual((p["start"], p["end"]), (12, 40))
        self.assertEqual(p["title"], "He landed a JET on the yacht")
        self.assertEqual(p["highlight"], ["jet", "the", "yacht"])
        self.assertEqual(p["hashtags"][:3], ["jet", "gta6", "yachtparty"])
        self.assertEqual(p["hashtags"].count("gta6"), 1)
        self.assertIn("fyp", p["hashtags"])

    def test_window_is_kept_inside_the_clip_and_the_limits(self):
        p = clips.clean_plan(dict(RAW, start=5, end=300), 60, CFG)
        self.assertEqual((p["start"], p["end"]), (5, 50))                    # no longer than max_seconds
        p = clips.clean_plan(dict(RAW, start=55, end=58), 60, CFG)
        self.assertEqual((p["start"], p["end"]), (45, 60))                   # too short: grown back from the end
        p = clips.clean_plan(dict(RAW, start="x", end=None), 20, CFG)
        self.assertEqual((p["start"], p["end"]), (0, 20))

    def test_bad_clips_are_skipped(self):
        with self.assertRaises(clips.Skip):
            clips.clean_plan({"ok": False, "why": "menu screen"}, 60, CFG)
        with self.assertRaises(clips.Skip):
            clips.clean_plan(RAW, 4, CFG)
        with self.assertRaises(clips.Skip):
            clips.clean_plan(dict(RAW, title="  "), 60, CFG)

    def test_caption_credits_the_streamer(self):
        p = clips.clean_plan(RAW, 60, CFG)
        self.assertTrue(clips.caption_for(p, "kai").startswith("would you bail? (clip: kai on Twitch) #jet"))
        self.assertNotIn("clip:", clips.caption_for(p, ""))
        self.assertIn("(clip: speed on YouTube)", clips.caption_for(p, "speed", "youtube"))

    def test_the_scouts_note_names_the_source(self):
        with tempfile.TemporaryDirectory() as d:
            clip = Path(d) / "@x yt-abc-10.mp4"
            (Path(d) / "@x yt-abc-10.json").write_text('{"credit": "IShowSpeed", "site": "youtube"}')
            self.assertEqual(clips.source_for(clip), ("IShowSpeed", "youtube"))
            self.assertEqual(clips.source_for(Path(d) / "@kai clip.mp4"), ("kai", "twitch"))

    def test_probe_reads_ffmpeg(self):
        out = ("  Duration: 00:01:02.50, start: 0.0\n  Stream #0:0[0x1](und): Video: h264 (High), yuv420p(tv), 1920x1080 [SAR 1:1], 60 fps\n"
               "  Stream #0:1[0x2](und): Audio: aac, 48000 Hz")
        self.assertEqual(clips.parse_probe(out), {"duration": 62.5, "w": 1920, "h": 1080, "audio": True})
        with self.assertRaises(clips.Skip):
            clips.parse_probe("Invalid data found")


class InboxTests(unittest.TestCase):
    def test_oldest_first_and_only_settled_videos(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            now = time.time()
            for name, age in (("b.mp4", 500), ("a.MOV", 900), ("new.mp4", 5), ("notes.txt", 900), (".hidden.mp4", 900)):
                (d / name).write_bytes(b"x")
                os.utime(d / name, (now - age, now - age))
            self.assertEqual([f.name for f in clips.inbox(d, now)], ["a.MOV", "b.mp4"])
            self.assertEqual(clips.inbox(d / "missing"), [])

    def test_credit_from_name_or_note(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(clips.credit_for(d / "@Kai_Cenat jet crash.mp4"), "Kai_Cenat")
            self.assertEqual(clips.credit_for(d / "jet crash.mp4"), "")
            (d / "jet crash.txt").write_text("@speed\nmore")
            self.assertEqual(clips.credit_for(d / "jet crash.mp4"), "speed")


class EditTests(unittest.TestCase):
    def test_command_ducks_the_game_under_the_hook(self):
        p = clips.clean_plan(RAW, 60, CFG)
        cmd = clips.edit_command(Path("in.mp4"), Path("l.png"), Path("o.mp4"), p, INFO, Path("v.wav"), 3.0)
        f = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("volume=0.22:enable='lt(t,3.35)'", f)
        self.assertIn("boxblur", f)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "12.00")
        plain = clips.edit_command(Path("in.mp4"), Path("l.png"), Path("o.mp4"), p, dict(INFO, w=1080, h=1920, audio=False))
        f = plain[plain.index("-filter_complex") + 1]
        self.assertNotIn("boxblur", f)                      # a tall clip fills the frame
        self.assertIn("[2:a]aresample", f)                  # no sound in the clip: silence stands in
        self.assertIn("anullsrc=r=48000:cl=stereo", plain)

    def test_title_layer_is_drawn(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            out = clips.overlay(clips.clean_plan(RAW, 60, CFG), "kai", Path(d) / "l.png")
            img = Image.open(out)
            self.assertEqual(img.size, (1080, 1920))
            box = img.getbbox()
            self.assertGreaterEqual(box[1], 200)            # under TikTok's top bar
            self.assertLess(box[3], 1500)                   # above the caption area
            self.assertEqual(img.crop((0, clips.PICTURE_Y + 5, 1080, clips.PICTURE_Y + clips.PICTURE_H - 5)).getbbox(), None)


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.inbox = d / "inbox"
        self.inbox.mkdir()
        self.patches = [patch.object(clips, "STATE", d / "clips.json"), patch.object(clips, "INBOX", self.inbox),
                        patch.object(clips, "SKIPPED", self.inbox / "skipped"), patch.object(clips, "settings", lambda: dict(CFG)),
                        patch.object(clips, "inbox", lambda: sorted(self.inbox.glob("*.mp4")))]
        for p in self.patches:
            p.start()
        self.svc = clips.ClipService(log=lambda m: None)
        self.svc._scout = lambda st: None                    # no fetching from the internet in tests
        self.noon = datetime(2026, 11, 19, 11, 5)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def _rec(self, src):
        return {"path": str(src) + ".out", "title": "t", "caption": "c", "src": str(src), "key": clips.fingerprint(src), "made": 1.0}

    def test_one_clip_per_slot_and_none_twice(self):
        (self.inbox / "a.mp4").write_bytes(b"aaa")
        with patch.object(clips, "make", side_effect=lambda src, cfg, log: self._rec(src)), \
                patch.object(self.svc, "deliver", return_value="posted") as deliver:
            self.assertEqual(self.svc.run_once(self.noon), "posted")
            self.assertEqual(self.svc.run_once(self.noon), "")              # the slot is used
            self.assertEqual(deliver.call_count, 1)
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 15, 31)), "empty")   # a.mp4 again: already posted
        self.assertTrue((self.inbox / "skipped" / "a.mp4").exists())

    def test_empty_inbox_keeps_the_slot_open(self):
        self.assertEqual(self.svc.run_once(self.noon), "empty")
        self.assertEqual(clips.wv.load_state(clips.STATE)["slots"], {})

    def test_a_skipped_clip_moves_aside_and_a_busy_gemini_waits(self):
        (self.inbox / "a.mp4").write_bytes(b"aaa")
        with patch.object(clips, "make", side_effect=clips.Skip("menu screen")):
            self.assertEqual(self.svc.run_once(self.noon), "skipped")
        self.assertTrue((self.inbox / "skipped" / "a.mp4").exists())
        (self.inbox / "b.mp4").write_bytes(b"bbb")
        with patch.object(clips, "make", side_effect=clips.Busy("down")):
            self.assertEqual(self.svc.run_once(self.noon), "waiting")
        self.assertTrue((self.inbox / "b.mp4").exists())
        self.assertEqual(self.svc.run_once(self.noon), "")                  # waiting out the 10 minutes

    def test_posted_everywhere_frees_the_files(self):
        src = self.inbox / "a.mp4"
        src.write_bytes(b"aaa")
        rec = self._rec(src)
        Path(rec["path"]).write_bytes(b"v")
        st = clips.wv.load_state(clips.STATE)
        st["videos"].append(rec)
        clips.wv.save_state(st, clips.STATE)
        with patch.object(self.svc._poster, "_deliver_tiktok", return_value="posted"), \
                patch.object(self.svc._poster, "post_instagram", return_value="posted"), \
                patch.object(self.svc._poster, "_text_result"), \
                patch.object(clips, "post_settings", lambda cfg: dict(cfg, instagram=True)):
            self.assertEqual(self.svc.deliver(rec, dict(CFG)), "posted")
        self.assertFalse(src.exists() or Path(rec["path"]).exists())
        self.assertTrue(clips.wv.load_state(clips.STATE)["videos"][0]["ig_posted"])


class ScoutTests(unittest.TestCase):
    NAMES = ["Grand Theft Auto VI", "GTA 6", "GTA VI", "GTA6"]

    def test_game_names_match_whole_words_only(self):
        from worker import clip_scout as cs
        self.assertTrue(cs.is_game("Grand Theft Auto VI", self.NAMES))
        self.assertTrue(cs.is_game("My first hour in GTA 6!! (crazy)", self.NAMES))
        self.assertFalse(cs.is_game("Grand Theft Auto V", self.NAMES))
        self.assertFalse(cs.is_game("GTA 67 leaks", self.NAMES))
        self.assertFalse(cs.is_game("Just Chatting", self.NAMES))

    def test_twitch_picks_are_new_watched_and_most_watched_first(self):
        from worker import clip_scout as cs
        es = [{"id": "1", "url": "u1", "view_count": 500, "duration": 30}, {"id": "2", "url": "u2", "view_count": 9000, "duration": 20},
              {"id": "3", "url": "u3", "view_count": 90000, "duration": 4}, {"id": "4", "url": "u4", "view_count": 10, "duration": 30},
              {"id": "5", "url": "u5", "view_count": 7000, "duration": 30}]
        self.assertEqual([e["id"] for e in cs.twitch_picks(es, {"tw:5"}, 300)], ["2", "1"])

    def test_moments_are_the_replay_peaks_after_the_intro(self):
        from worker import clip_scout as cs
        heat = [{"start_time": i * 10.0, "end_time": i * 10.0 + 10, "value": v} for i, v in
                enumerate([1.0, .9, .2, .2, .2, .2, .2, .2, .2, .2, .3, .8, .7, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .2, .6])]
        self.assertEqual(cs.moments(heat, 300, 2), [(86, 144), (255, 300)])     # the intro peak is left out
        self.assertEqual(cs.moments([], 300), [])

    def test_geminis_moments_are_kept_inside_the_video(self):
        from worker import clip_scout as cs
        raw = {"is_game": True, "moments": [{"start": 100, "end": 400}, {"start": 150, "end": 170}, {"start": 590, "end": 640},
                                            {"start": 900, "end": 950}, "x"]}
        self.assertEqual(cs.clean_moments(raw, 600, 3), [(100, 175)])            # too long is trimmed; overlap, tail and out-of-range dropped
        self.assertEqual(cs.clean_moments(dict(raw, is_game=False), 600, 3), [])
        self.assertEqual(cs.clean_moments(None, 600, 3), [])

    def test_videos_must_be_the_game_and_not_a_compilation(self):
        from worker import clip_scout as cs
        ok = {"title": "I robbed a bank in GTA 6", "duration": 900, "view_count": 80000}
        skip = ["meme", "compilation"]
        self.assertTrue(cs.video_ok(ok, self.NAMES, skip, 50000))
        self.assertFalse(cs.video_ok(dict(ok, title="GTA 6 memes"), self.NAMES, skip, 50000))
        self.assertFalse(cs.video_ok(dict(ok, title="GTA 5 heist"), self.NAMES, skip, 50000))
        self.assertFalse(cs.video_ok(dict(ok, view_count=100), self.NAMES, skip, 50000))
        self.assertFalse(cs.video_ok(dict(ok, live_status="is_live"), self.NAMES, skip, 50000))

    def test_a_full_inbox_fetches_nothing(self):
        from worker import clip_scout as cs
        with tempfile.TemporaryDirectory() as d:
            with patch.object(clips, "STATE", Path(d) / "clips.json"), patch.object(clips, "inbox", lambda **k: [1, 2, 3]), \
                    patch.object(cs.Scout, "twitch", side_effect=AssertionError), patch.object(cs.Scout, "youtube", side_effect=AssertionError):
                self.assertEqual(cs.Scout(lambda m: None).run(dict(cs.DEFAULTS, stock=3), now=1000.0), 0)
                st = clips.wv.load_state(clips.STATE)
                on = dict(cs.DEFAULTS, start_date="")
                self.assertFalse(cs.Scout().due(on, st, 1000.0 + 600))
                self.assertTrue(cs.Scout().due(on, st, 1000.0 + 1801))
                self.assertFalse(cs.Scout().due(dict(on, start_date="2999-01-01"), st, 1000.0 + 1801))   # not before launch


if __name__ == "__main__":
    unittest.main()
