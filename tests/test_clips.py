"""Game clips: the inbox, the cut plan, the ffmpeg call and the daily slots."""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from worker import clips
from worker import facecam

CFG = dict(clips.DEFAULTS, times="11:00,14:00,15:30,19:30,20:30")   # the slot tests are written around these times
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
        self.assertLessEqual(len(p["hashtags"]), 4)

    def test_window_is_kept_inside_the_clip_and_the_limits(self):
        p = clips.clean_plan(dict(RAW, start=5, end=300), 60, CFG)
        self.assertEqual((p["start"], p["end"]), (5, 35))                    # no longer than max_seconds
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

    def test_captions_group_words_and_wait_for_the_narrator(self):
        words = [(0.2, 0.5, "Watch", .9), (0.5, 0.8, "this.", .9), (3.0, 3.2, "I", .1), (3.2, 3.5, "don't", .9), (3.5, 3.8, "know", .9),
                 (3.8, 4.0, "what", .9), (4.0, 4.2, "happened,", .9), (6.0, 6.4, "unbelievable", .9), (9.9, 10.5, "late", .9)]
        ch = clips.caption_chunks(words, 10.0, skip_before=2.5)
        self.assertEqual([c[2] for c in ch], ["I DON'T KNOW", "WHAT HAPPENED", "UNBELIEVABLE"])   # a word in the last tenth of a second is left off
        self.assertEqual(ch[0][:2], (3.0, 3.8))                 # held until the next caption starts
        self.assertLessEqual(ch[-1][1], 10.0)
        self.assertEqual(clips.caption_chunks(words, 10.0)[0][2], "WATCH THIS")

    def test_guessed_speech_gets_no_captions(self):
        noise = [(i * .5, i * .5 + .4, "guwaku", .05 if i % 3 else .8) for i in range(12)]
        self.assertEqual(clips.caption_chunks(noise, 10.0), [])
        self.assertEqual(clips.caption_chunks([(0, 1, "hi", .9)], 10.0), [])

    def test_subtitle_file_and_burn_in(self):
        with tempfile.TemporaryDirectory() as d:
            f = clips.write_ass([(1.5, 2.25, "OH NO {x}"), (61.0, 62.0, "GO")], Path(d) / "c.ass")
            text = f.read_text()
            self.assertIn("Dialogue: 0,0:00:01.50,0:00:02.25,Cap", text)
            self.assertIn("0:01:01.00", text)
            self.assertTrue(text.rstrip().splitlines()[-2].endswith("OH NO x"))
            p = clips.clean_plan(RAW, 60, CFG)
            cmd = clips.edit_command(Path("in.mp4"), Path("l.png"), Path("o.mp4"), p, INFO, subs=f)
            self.assertIn(f"subtitles=f={f}:fontsdir=", cmd[cmd.index("-filter_complex") + 1])

    def test_title_layer_is_drawn(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            out = clips.overlay(clips.clean_plan(RAW, 60, CFG), "kai", Path(d) / "l.png")
            img = Image.open(out)
            self.assertEqual(img.size, (1080, 1920))
            box = img.getbbox()
            self.assertGreaterEqual(box[1], 200)            # under TikTok's top bar
            self.assertLess(box[3], 1700)                   # the credit sits just under the picture
            self.assertEqual(img.crop((0, clips.PICTURE_Y + 5, 1080, clips.PICTURE_Y + clips.PICTURE_H - 5)).getbbox(), None)


class FacecamTests(unittest.TestCase):
    def _stream(self, n=8):
        """Frames of a stream: game (changing every frame) with a still webcam at x 0-300, y 280-450, bordered, and a
        line in the streamer's room at x 220 that must not be taken for the webcam's edge."""
        import numpy as np
        rng = np.random.default_rng(1)
        room = rng.integers(60, 90, (170, 300)).astype(np.uint8)
        room[:, 218:222] = 200
        out = []
        for _ in range(n):
            g = rng.integers(0, 255, (720, 1280)).astype(np.uint8)
            g[280:450, 0:300] = room
            g[278:280, 0:302] = 255
            g[278:452, 300:302] = 255
            out.append(g)
        return out

    def test_webcam_box_stops_at_the_border_not_at_a_line_in_the_room(self):
        x, y, w, h = facecam.webcam_box(self._stream(), (110.0, 360.0, 50.0))
        self.assertEqual(x, 0)                                  # the webcam sits on the frame's edge
        self.assertTrue(296 <= x + w <= 304, (x, w))            # not at the room's line (220)
        self.assertTrue(276 <= y <= 282, y)

    def test_crop_fits_the_panel_inside_the_webcam_with_the_face_in_it(self):
        x, y, w, h = facecam.cam_crop((0, 282, 298, 168), (110.0, 378.0, 62.0), clips.W / clips.CAM_H)
        self.assertAlmostEqual(w / h, clips.W / clips.CAM_H, delta=.05)
        self.assertTrue(x >= 0 and y >= 282 and x + w <= 298 and y + h <= 450)
        self.assertTrue(y < 378 < y + h and x < 110 < x + w)

    def test_webcam_goes_on_top_of_the_game(self):
        p = clips.clean_plan(RAW, 60, CFG)
        cmd = clips.edit_command(Path("in.mp4"), Path("l.png"), Path("o.mp4"), p, INFO, cam=(10, 20, 300, 140))
        f = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn(f"crop=300:140:10:20,scale=1080:{clips.CAM_H}", f)
        self.assertIn(f"overlay=0:{clips.PICTURE_Y}", f)
        self.assertIn(f"overlay=0:{clips.GAME_Y}", f)
        self.assertEqual(clips.GAME_Y + clips.GAME_H, clips.PICTURE_Y + clips.PICTURE_H)   # captions/credit stay put


from worker import clip_scout as cs


class LoudTests(unittest.TestCase):
    def test_loud_moments_are_found_loudest_first_and_kept_apart(self):
        level = [-40.0 + (i % 3) * .5 for i in range(600)]
        for i in (200, 201, 202):
            level[i] = -12.0
        for i in (220, 221):
            level[i] = -14.0                                 # the same moment, still echoing
        for i in (450, 451, 452):
            level[i] = -18.0
        self.assertEqual(cs.loud_moments(level, count=3), [(179, 211), (429, 461)])
        self.assertEqual(cs.loud_moments([-40.0] * 600), [])                 # a calm stream gives nothing
        self.assertEqual(cs.loud_moments([-40.0] * 20), [])                  # too little to judge

    def test_a_live_recording_is_cut_to_the_segments_that_cover_the_stretch(self):
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-MAP:URI=\"init.mp4\"\n" + "".join(f"#EXTINF:10.000,\n{i}.mp4\n" for i in range(10))
        out, first = cs.cut_playlist(text, "https://x/a/", 25, 41)
        self.assertEqual(first, 20.0)
        self.assertEqual([l for l in out.splitlines() if l.startswith("https")], ["https://x/a/2.mp4", "https://x/a/3.mp4", "https://x/a/4.mp4"])
        self.assertIn('URI="https://x/a/init.mp4"', out)
        self.assertTrue(out.strip().endswith("#EXT-X-ENDLIST"))
        with self.assertRaises(RuntimeError):
            cs.cut_playlist(text, "https://x/a/", 500, 520)

    def test_a_moment_in_a_live_recording_is_placed_in_its_pieces_and_taken_once(self):
        self.assertEqual(cs.place([20, 19, 20], 0), (0, 0))
        self.assertEqual(cs.place([20, 19, 20], 25), (1, 5))
        self.assertEqual(cs.place([20, 19, 20], 39), (2, 0))
        names = [f"{i:06d}.ts" for i in range(10, 20)]
        levels = {n: [-40.0 + (i % 3) * .5 for i in range(20)] for n in names}
        levels[names[5]][3:6] = [-12.0] * 3                  # a scream 4 s into the sixth piece (second 104 of the run)
        got = cs.tap_moments(names, levels, -1e9, 2)
        self.assertEqual(got, [(4, 5, 2, 32, 14 * cs.SEG + 2)])              # starts 22 s earlier, in the fifth piece
        self.assertEqual(cs.tap_moments(names, levels, got[0][4], 2), [])    # not cut a second time on the next look

    def test_a_live_stream_is_recorded_with_its_sound(self):
        v = lambda h, a="mp4a": {"protocol": "m3u8_native", "height": h, "vcodec": "avc1", "acodec": a, "url": f"v{h}"}
        self.assertEqual(cs.tap_inputs({"formats": [v(1080), v(720), v(360)]}), ["v720"])
        sound = {"protocol": "m3u8_native", "vcodec": "none", "acodec": "mp4a", "abr": 128, "url": "snd"}
        self.assertEqual(cs.tap_inputs({"formats": [v(1080, "none"), v(480, "none"), sound]}), ["v480", "snd"])
        with self.assertRaises(RuntimeError):
            cs.tap_inputs({"formats": []})

    def test_live_moments_post_right_away_but_spaced_and_capped(self):
        now = datetime(2026, 11, 19, 16, 0)
        cfg = dict(clips.DEFAULTS)
        self.assertTrue(clips.live_ok(cfg, {}, now))
        t = now.timestamp()
        self.assertFalse(clips.live_ok(cfg, {"live_posts": {"2026-11-19": [t - 600]}}, now))
        self.assertTrue(clips.live_ok(cfg, {"live_posts": {"2026-11-19": [t - 3600]}}, now))
        self.assertFalse(clips.live_ok(cfg, {"live_posts": {"2026-11-19": [t - 3600 * i for i in range(1, 13)]}}, now))
        self.assertTrue(clips.live_ok(cfg, {"live_posts": {"2026-11-18": [t - 60]}}, now))
        self.assertFalse(clips.live_ok(dict(cfg, live_now=False), {}, now))


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

    def test_a_live_moment_posts_between_the_set_times_and_an_ordinary_clip_waits(self):
        between = datetime(2026, 11, 19, 12, 40)
        (self.inbox / "a.mp4").write_bytes(b"aaa")
        with patch.object(clips, "make", side_effect=lambda src, cfg, log: self._rec(src)), \
                patch.object(self.svc, "deliver", return_value="posted") as deliver:
            st = clips.wv.load_state(clips.STATE)
            st["slots"] = {"2026-11-19": ["11:00"]}
            clips.wv.save_state(st, clips.STATE)
            self.assertEqual(self.svc.run_once(between), "")                # an ordinary clip waits for its time
            (self.inbox / "@kai live-1-50.mp4").write_bytes(b"live1")
            (self.inbox / "@kai live-1-900.mp4").write_bytes(b"live2")
            self.assertEqual(self.svc.run_once(between), "posted")
            self.assertEqual(Path(deliver.call_args[0][0]["src"]).name, "@kai live-1-50.mp4")
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 12, 50)), "")       # the next one is spaced out
            self.assertEqual(deliver.call_count, 1)
        self.assertEqual(clips.wv.load_state(clips.STATE)["slots"], {"2026-11-19": ["11:00"]})

    def test_missed_times_are_made_up_one_by_one_and_spaced_out(self):
        for n in "abc":
            (self.inbox / f"{n}.mp4").write_bytes(n.encode() * 3)
        woke = datetime(2026, 11, 19, 21, 0)                # asleep all day: 11:00 ... 20:30 all missed
        with patch.object(clips, "make", side_effect=lambda src, cfg, log: self._rec(src)), \
                patch.object(self.svc, "deliver", return_value="posted") as deliver:
            self.assertEqual(self.svc.run_once(woke), "posted")
            self.assertEqual(clips.wv.load_state(clips.STATE)["slots"], {"2026-11-19": ["11:00"]})   # one time, not all
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 21, 20)), "")      # too soon after the last
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 21, 41)), "posted")
            self.assertEqual(deliver.call_count, 2)
        self.assertEqual(clips.wv.load_state(clips.STATE)["slots"], {"2026-11-19": ["11:00", "14:00"]})

    def test_a_failed_upload_is_tried_again_before_the_clip_is_given_up(self):
        (self.inbox / "a.mp4").write_bytes(b"aaa")
        (self.inbox / "b.mp4").write_bytes(b"bbb")
        made = []

        def make(src, cfg, log):
            rec = self._rec(src)
            Path(rec["path"]).write_bytes(b"video")
            made.append(src.name)
            return rec
        with patch.object(clips, "make", side_effect=make), \
                patch.object(self.svc, "deliver", side_effect=["retry", "retry", "posted"]) as deliver:
            self.assertEqual(self.svc.run_once(self.noon), "retry")
            st = clips.wv.load_state(clips.STATE)
            self.assertEqual(st["slots"], {})                                  # the time stays open
            self.assertEqual(st["pending"]["tries"], 1)
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 11, 10)), "")      # waits retry_min
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 11, 16)), "retry")
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 19, 11, 27)), "posted")
            self.assertFalse(deliver.call_args_list[0].kwargs["last_try"])
            self.assertTrue(deliver.call_args_list[2].kwargs["last_try"])     # the third try is the last
        self.assertEqual(made, ["a.mp4"])                                      # cut once, uploaded three times
        st = clips.wv.load_state(clips.STATE)
        self.assertEqual(st["slots"], {"2026-11-19": ["11:00"]})
        self.assertNotIn("pending", st)

    def test_youtube_failure_asks_for_a_retry_until_the_last_try(self):
        from worker import guides
        src = self.inbox / "a.mp4"
        src.write_bytes(b"aaa")
        rec = self._rec(src)
        Path(rec["path"]).write_bytes(b"video")
        cfg = dict(CFG, tiktok=False, youtube=True)
        texts = []
        with patch.object(clips, "post_settings", return_value={"post": True, "instagram": False}), \
                patch.object(guides, "youtube_upload", side_effect=RuntimeError("Timeout 60000ms")), \
                patch("core.phone_alerts.send_text", side_effect=lambda t, *a, **k: texts.append(t)):
            self.assertEqual(self.svc.deliver(rec, cfg, last_try=False), "retry")
            self.assertEqual(texts, [])                                     # no text for a try that will be repeated
            self.assertTrue(src.exists() and Path(rec["path"]).exists())
            self.assertEqual(self.svc.deliver(rec, cfg, last_try=True), "failed")
        self.assertEqual(len(texts), 1)
        self.assertTrue((self.inbox / "skipped" / "a.mp4").exists())        # given up: never cut again

    def test_a_video_youtube_is_still_checking_is_not_uploaded_again(self):
        from worker import guides
        src = self.inbox / "a.mp4"
        src.write_bytes(b"aaa")
        rec = self._rec(src)
        with patch.object(clips, "post_settings", return_value={"post": True, "instagram": False}), \
                patch.object(guides, "youtube_upload", side_effect=guides.StillChecking("saved as private")), \
                patch("core.phone_alerts.send_text"):
            self.assertEqual(self.svc.deliver(rec, dict(CFG, tiktok=False, youtube=True), last_try=False), "failed")

    def test_a_retry_that_ends_after_midnight_keeps_the_new_days_times(self):
        src = self.inbox / "a.mp4"
        src.write_bytes(b"aaa")
        rec = self._rec(src)
        Path(rec["path"]).write_bytes(b"video")
        st = clips.wv.load_state(clips.STATE)
        st["slots"] = {"2026-11-20": ["11:00"]}
        st["pending"] = {"rec": rec, "tries": 1, "day": "2026-11-19", "slot": "20:30", "live": False, "next": 0}
        clips.wv.save_state(st, clips.STATE)
        with patch.object(self.svc, "deliver", return_value="posted"):
            self.assertEqual(self.svc.run_once(datetime(2026, 11, 20, 11, 30)), "posted")
        self.assertEqual(clips.wv.load_state(clips.STATE)["slots"], {"2026-11-20": ["11:00"]})

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
            self.assertEqual(self.svc.deliver(rec, dict(CFG, youtube=False)), "posted")   # never a real upload from a test
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
                self.assertFalse(cs.Scout().due(dict(on, start_date="2999-01-01", prelaunch=False), st, 1000.0 + 1801))   # not before launch
                self.assertTrue(cs.Scout().due(dict(on, start_date="2999-01-01"), st, 1000.0 + 1801))    # ...unless streamers' own videos are wanted


if __name__ == "__main__":
    unittest.main()
