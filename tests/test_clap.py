import time
import unittest

import numpy as np

from core import clap

RATE = clap.SAMPLE_RATE


class DoubleClapTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)

    def room(self, seconds):
        return self.rng.normal(0, 0.0005, int(seconds * RATE)).astype(np.float32)

    def clap(self, amp=0.6, ring=True):
        """A sharp bright burst, then (like a real room) a quieter ring for ~0.12 s."""
        n = int(0.03 * RATE)
        burst = self.rng.normal(0, amp, n) * np.exp(-np.arange(n) / (0.006 * RATE))
        if ring:
            m = int(0.12 * RATE)
            burst = np.concatenate([burst, self.rng.normal(0, amp * 0.06, m)
                                    * np.exp(-np.arange(m) / (0.05 * RATE))])
        return burst.astype(np.float32)

    def voice(self, seconds, amp=0.1):
        t = np.arange(int(seconds * RATE)) / RATE
        return (amp * np.sin(2 * np.pi * 160 * t) * (1 + 0.5 * np.sin(2 * np.pi * 4 * t))).astype(np.float32)

    def fires(self, *parts):
        tracker = clap.DoubleClapTracker()
        audio = np.concatenate(parts)
        # Fed in 1024-sample blocks, the size the mic callback delivers.
        return sum(tracker.push(audio[i:i + 1024]) for i in range(0, len(audio), 1024))

    def test_double_clap_and_quiet_double_snap_fire_once(self):
        self.assertEqual(self.fires(self.room(2), self.clap(), self.room(0.35), self.clap(), self.room(1.2)), 1)
        self.assertEqual(self.fires(self.room(2), self.clap(0.08), self.room(0.45),
                                    self.clap(0.08), self.room(1.2)), 1)

    def test_room_ring_after_each_clap_does_not_cancel_the_pair(self):
        # The bug found on real audio: the ring after a snap counted as noise.
        self.assertEqual(self.fires(self.room(2), self.clap(ring=True), self.room(0.3),
                                    self.clap(ring=True), self.room(1.2)), 1)

    def test_single_triple_and_spread_out_claps_do_not_fire(self):
        self.assertEqual(self.fires(self.room(2), self.clap(), self.room(1.5)), 0)
        self.assertEqual(self.fires(self.room(2), self.clap(), self.room(0.3), self.clap(),
                                    self.room(0.3), self.clap(), self.room(1.5)), 0)
        self.assertEqual(self.fires(self.room(2), self.clap(), self.room(1.2), self.clap(), self.room(1.5)), 0)

    def test_speech_and_claps_inside_speech_do_not_fire(self):
        self.assertEqual(self.fires(self.room(2), self.voice(2), self.room(1.5)), 0)
        self.assertEqual(self.fires(self.room(2), self.voice(0.3), self.clap(), self.room(0.3),
                                    self.clap(), self.room(1.2)), 0)

    def test_cooldown_stops_a_quick_repeat(self):
        pair = [self.clap(), self.room(0.35), self.clap(), self.room(0.8)]
        self.assertEqual(self.fires(self.room(2), *pair, *pair, self.room(1)), 1)

    def test_detector_feed_never_raises_and_calls_back(self):
        hits = []
        det = clap.ClapDetector(on_detect=lambda: hits.append(1), logger=lambda m: None)
        det.feed(np.zeros((1024, 1), np.int16))       # not started: ignored
        det.start()
        audio = np.concatenate([self.room(2), self.clap(), self.room(0.35), self.clap(), self.room(1.2)])
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).reshape(-1, 1)
        for i in range(0, len(pcm), 1024):
            det.feed(pcm[i:i + 1024])
        det.feed("not audio")
        for _ in range(100):
            if hits:
                break
            time.sleep(0.02)
        det.stop()
        self.assertEqual(hits, [1])


if __name__ == "__main__":
    unittest.main()
