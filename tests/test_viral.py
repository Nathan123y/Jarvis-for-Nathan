"""Viral videos: script cleaning, beat timing, the daily slots, the audio mix and the scene-code parsing."""
import json
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from unittest.mock import patch

from worker import viral as wv

SCRIPT = {"topic": "neutron star", "title": "What if a spoonful of neutron star landed here?",
          "highlight": ["spoonful of neutron star"], "set": "backyard",
          "beats": [{"say": "A teaspoon of neutron star just landed.", "emph": ["teaspoon"], "sfx": "boom", "scene": "impact",
                     "hud": {"label": "ITS MASS", "kind": "count", "from": 0, "to": "1000000000", "unit": " t"}},
                    {"say": "It weighs about a billion tons.", "sfx": "nonsense", "scene": "x"},
                    {"say": "The ground can't hold it up.", "sfx": "rumble", "scene": "x",
                     "hud": {"kind": "count", "from": 0, "to": 1e20, "sub": "TOO BIG"}},
                    {"say": "Everything nearby slides in.", "sfx": "whoosh", "scene": "x"},
                    {"say": "", "sfx": "none"},
                    {"say": "Rewind. Here's the real version.", "sfx": "riser", "scene": "x", "hud": {"kind": "weird", "text": "HERE"}}],
          "end_fact": "A neutron star packs more mass than the Sun into 20 km.",
          "caption": "Would you survive this?", "hashtags": ["#FYP", "Science!", "space"]}


class ScriptTests(unittest.TestCase):
    def test_clean_script_adds_the_hook_as_beat_zero_and_tidies(self):
        s = wv.clean_script(SCRIPT, library={})
        self.assertEqual(s["beats"][0]["say"], SCRIPT["title"])
        self.assertEqual(len(s["beats"]), 6)                     # empty line dropped, hook added
        self.assertEqual(s["beats"][2]["sfx"], "none")           # unknown sfx -> none
        self.assertEqual(s["beats"][1]["hud"]["to"], 1e9)        # numbers parsed
        self.assertEqual(s["beats"][3]["hud"]["kind"], "text")   # a 21-digit counter becomes text
        self.assertEqual(s["beats"][5]["hud"]["kind"], "text")
        self.assertEqual(s["hashtags"], ["fyp", "science", "space"])
        self.assertEqual(s["beats"][1]["ambience"], "keep")                 # unknown sound ids fall back

    def test_clean_script_keeps_known_sound_ids(self):
        lib = {"amb1": {"kind": "ambient"}, "hit1": {"kind": "oneshot"}}
        raw = dict(SCRIPT, beats=[dict(b, ambience="amb1", sfx="hit1") for b in SCRIPT["beats"]])
        s = wv.clean_script(raw, library=lib)
        self.assertEqual((s["beats"][1]["ambience"], s["beats"][1]["sfx"]), ("amb1", "hit1"))
        raw = dict(SCRIPT, beats=[dict(b, ambience="hit1") for b in SCRIPT["beats"]])     # a one-shot can't be a bed
        self.assertEqual(wv.clean_script(raw, library=lib)["beats"][1]["ambience"], "keep")

    def test_extra_hits_and_levels_are_cleaned(self):
        lib = {"roar": {"kind": "ambient"}, "swish": {"kind": "oneshot"}}
        raw = dict(SCRIPT, beats=[dict(b, level="9", sfx2=[{"id": "swish", "at": 1.2}, "nope", {"id": "boom", "at": "x"}])
                                  for b in SCRIPT["beats"]])
        b = wv.clean_script(raw, library=lib)["beats"][1]
        self.assertEqual(b["level"], 2.5)
        self.assertEqual(b["sfx2"], [{"id": "swish", "at": 1.2}, {"id": "boom", "at": 0.0}])

    def test_captions_stay_up_long_enough_to_read(self):
        ls = wv.caption_lengths([{"say": "What if humans evolved 1,000 years every 3 seconds?"}, {"say": "Gone."},
                                 {"say": " ".join(["word"] * 30)}])
        self.assertEqual(ls[1], 1.5)
        self.assertGreaterEqual(ls[0], 2.6)
        self.assertEqual(ls[2], 3.6)

    def test_clean_script_refuses_a_stub(self):
        with self.assertRaises(ValueError):
            wv.clean_script({"title": "x", "beats": [{"say": "a"}]})

    def test_json_and_code_are_pulled_out_of_fenced_replies(self):
        self.assertEqual(wv._json_from('sure!\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIn("export default", wv._code_from("here\n```js\nexport default function (K) {}\n```"))
        with self.assertRaises(ValueError):
            wv._code_from("I can't do that")

    def test_write_script_retries_a_bad_reply(self):
        replies = iter(["not json", json.dumps(SCRIPT)])
        with patch.object(wv, "sound_library", return_value=[]):
            s = wv.write_script([], "", ask=lambda prompt, timeout_s=0: next(replies))
        self.assertEqual(s["title"], SCRIPT["title"])


class TimingTests(unittest.TestCase):
    def test_timeline_follows_the_speech_with_a_long_enough_hook(self):
        beats, total = wv.timeline([1.0, 2.0, 0.5])
        self.assertEqual(beats[0], (0.25, 3.45))                 # hook held for 3.2 s
        self.assertAlmostEqual(beats[1][1] - beats[1][0], 2.38)
        self.assertAlmostEqual(beats[2][1] - beats[2][0], 1.6)   # short lines still get 1.6 s
        self.assertAlmostEqual(total, beats[-1][1] + 0.4 + 2.6)

    def test_review_frames_cover_the_whole_video(self):
        times, total = wv.timeline([2.0] * 15)
        picks = wv.review_times(times, total)
        self.assertEqual(len(picks), 8)
        self.assertLess(picks[0], 3)
        self.assertGreater(picks[-1], times[-1][0])

    def test_due_slots_skips_done_and_stale_ones(self):
        now = datetime(2026, 11, 2, 16, 30)
        self.assertEqual(wv.due_slots("11:00,16:00,20:30", [], now), ["16:00"])          # 11:00 is 5.5 h stale
        self.assertEqual(wv.due_slots("11:00,16:00,20:30", ["16:00"], now), [])
        self.assertEqual(wv.due_slots("bad, 16:15", [], now), ["16:15"])


class StatsTests(unittest.TestCase):
    def test_counts_parse(self):
        self.assertEqual([wv.parse_count(x) for x in ("987", "1.2K", "3.4M", "12,345", "")], [987, 1200, 3400000, 12345, 0])

    def test_a_milestone_is_texted_once(self):
        stats = [{"id": "1", "views": "12.5K", "desc": "Could LeBron beat Satan?", "url": "u1"},
                 {"id": "2", "views": "300", "desc": "Jupiter", "url": "u2"}]
        msgs, seen = wv.viral_news(stats, {}, 0)
        self.assertEqual(len(msgs), 1)
        self.assertIn("10,000", msgs[0])
        self.assertIn("LeBron", msgs[0])
        msgs, seen = wv.viral_news(stats, seen, 10)
        self.assertEqual(msgs, [])

    def test_far_above_usual_is_flagged(self):
        stats = [{"id": str(i), "views": "200"} for i in range(6)] + [{"id": "x", "views": "2.5K", "desc": "Moon"}]
        msgs, _ = wv.viral_news(stats, {}, 0)
        self.assertTrue(any("5x" in m for m in msgs))


class AudioTests(unittest.TestCase):
    def test_mix_writes_a_normalised_wav_of_the_right_length(self):
        s = wv.clean_script(SCRIPT, library={})
        voice = [np.sin(np.linspace(0, 300, wv.SR // 2)).astype(np.float32) for _ in s["beats"]]
        beats, total = wv.timeline([0.5] * len(s["beats"]))
        with tempfile.TemporaryDirectory() as d:
            out = wv.mix(voice, beats, s, total, Path(d) / "a.wav", seed=3)
            with wave.open(str(out)) as w:
                self.assertEqual(w.getframerate(), wv.SR)
                self.assertAlmostEqual(w.getnframes() / wv.SR, total, places=1)
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            self.assertLessEqual(np.abs(x).max(), 32767 * 0.9)
            self.assertGreater(np.abs(x).max(), 32767 * 0.5)

    def test_one_recording_is_cut_into_its_lines_at_the_pauses(self):
        rate = 24000
        tone = lambda sec: (np.sin(np.arange(int(rate * sec)) * .2) * .5).astype(np.float32)
        gap = lambda sec: np.zeros(int(rate * sec), np.float32)
        x = np.concatenate([tone(1.0), gap(.5), tone(2.0), gap(.1), tone(.3), gap(.6), tone(1.5)])   # the .1 s dip is mid-line
        pieces = wv.split_by_pauses(x, rate, [4, 9, 6])
        self.assertEqual(len(pieces), 3)
        self.assertAlmostEqual(len(pieces[0]) / rate, 1.25, delta=.05)
        self.assertIsNone(wv.split_by_pauses(x, rate, [1, 1, 30]))          # pieces that don't fit the words
        self.assertIsNone(wv.split_by_pauses(tone(3), rate, [2, 2]))       # no pause at all

    def test_caption_only_mix_has_sound_without_a_voice(self):
        s = wv.clean_script(SCRIPT, library={})
        s["beats"][1]["sfx"] = "boom"
        s["beats"][2]["sfx2"] = [{"id": "hit", "at": .5}]
        s["beats"][3]["level"] = 2.0
        beats, total = wv.timeline(wv.caption_lengths(s["beats"]), hook_min=2.6, gap=0)
        with tempfile.TemporaryDirectory() as d, patch.object(wv, "sound_library", return_value=[]):
            out = wv.mix(None, beats, s, total, Path(d) / "a.wav", seed=1)
            with wave.open(str(out)) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        self.assertGreater(np.abs(x).max(), 10000)

    def test_every_sound_effect_renders(self):
        for k in wv.SFX:
            self.assertTrue(np.isfinite(wv.sfx(k, 1)).all(), k)


if __name__ == "__main__":
    unittest.main()
