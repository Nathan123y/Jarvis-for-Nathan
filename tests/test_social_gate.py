"""worker/social_gate.py: caps, gaps, topics, duplicates, freezes, lock pages, jitter."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import social_gate as gate

CFG = {"accounts": {"instagram": {"niche": "stories", "per_day": 2, "gap_min": 180}}, "recheck_h": 6}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "gate.json"
        self.video = Path(self.tmp.name) / "v.mp4"
        self.video.write_bytes(b"abc")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, niche="stories", video=None, now=None):
        gate.check("instagram", niche, video, now=now, cfg=CFG, path=self.state)

    def test_topic(self):
        with self.assertRaises(gate.Blocked):
            self.check("gta")
        self.check("stories")

    def test_gap_cap_and_duplicate(self):
        t = time.mktime((2026, 11, 24, 9, 0, 0, 0, 0, -1))
        gate.record("instagram", self.video, now=t, path=self.state)
        with self.assertRaises(gate.Blocked):                       # 1 h later: inside the 3 h gap
            self.check(now=t + 3600)
        with self.assertRaises(gate.Blocked):                       # the same file again
            self.check(video=self.video, now=t + 4 * 3600)
        self.check(now=t + 4 * 3600)
        gate.record("instagram", None, now=t + 4 * 3600, path=self.state)
        with self.assertRaises(gate.Blocked):                       # third post of the day
            self.check(now=t + 8 * 3600)

    def test_freeze_texts_once_and_lifts_on_a_post(self):
        texts = []
        gate.freeze("instagram", "Enter confirmation code", lambda m: None, path=self.state, text=texts.append)
        gate.freeze("instagram", "Enter confirmation code", lambda m: None, path=self.state, text=texts.append)
        self.assertEqual(len(texts), 1)
        with self.assertRaises(gate.Blocked):
            self.check()
        self.check(now=time.time() + 7 * 3600)                     # looked at again after recheck_h
        gate.record("instagram", None, path=self.state)
        self.check(now=time.time() + 4 * 3600)

    def test_lock_pages(self):
        self.assertTrue(gate.lock_reason("https://www.instagram.com/accounts/suspended/?next=x", "Enter confirmation code"))
        self.assertTrue(gate.lock_reason("https://www.tiktok.com/foryou", "Verify to continue"))
        self.assertEqual(gate.lock_reason("https://www.instagram.com/", "Home Reels Create"), "")

    def test_jitter_is_steady_and_small(self):
        a = gate.jittered("11:00,17:00", "2026-11-24")
        self.assertEqual(a, gate.jittered("11:00,17:00", "2026-11-24"))
        for got, want in zip(a.split(","), (660, 1020)):
            h, m = map(int, got.split(":"))
            self.assertLessEqual(abs(h * 60 + m - want), 20)


if __name__ == "__main__":
    unittest.main()


class ChromeTests(unittest.TestCase):
    def test_which_chrome_is_running(self):
        prof = Path("/x/memory/tiktok_profile")
        ours = f"/Chrome --user-data-dir={prof} --remote-debugging-port=9301 --no-first-run"
        old = "/Chrome --user-data-dir=/x/memory/tiktok_profile_old_20261124 --remote-debugging-port=9301"
        with patch.object(gate, "_alive", return_value=True):
            with patch.object(gate, "_commands", return_value=[ours, ours + " --type=renderer"]):
                self.assertEqual(gate.running(prof), "ours")
            with patch.object(gate, "_commands", return_value=[old]):
                self.assertEqual(gate.running(prof), "")
            with patch.object(gate, "_commands", return_value=[f"/Chrome --user-data-dir={prof} --enable-automation"]):
                self.assertEqual(gate.running(prof), "other")

    def test_fixed_ports(self):
        self.assertEqual(gate.port_for(Path("/a/instagram_profile")), 9302)
        self.assertTrue(9310 <= gate.port_for(Path("/a/other")) < 9390)
