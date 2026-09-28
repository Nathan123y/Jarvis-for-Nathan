import unittest
from unittest.mock import patch

from core.playback_timing import playback_idle


class PlaybackTimingTests(unittest.TestCase):
    def test_network_gap_does_not_drain_scheduled_speaker_audio(self):
        with patch("core.playback_timing.time.monotonic", return_value=10.25), \
             patch("core.playback_timing.time.time", return_value=100.25):
            self.assertFalse(playback_idle(10.0, 100.40, 0.18))

    def test_short_gap_does_not_force_new_prebuffer(self):
        with patch("core.playback_timing.time.monotonic", return_value=10.10), \
             patch("core.playback_timing.time.time", return_value=100.30):
            self.assertFalse(playback_idle(10.0, 100.20, 0.18))

    def test_drained_speaker_can_be_reprimed_or_mic_released(self):
        with patch("core.playback_timing.time.monotonic", return_value=10.80), \
             patch("core.playback_timing.time.time", return_value=100.80):
            self.assertTrue(playback_idle(10.0, 100.20, 0.18))
            self.assertTrue(playback_idle(10.0, 100.20, 0.65))

    def test_no_audio_yet_is_not_an_idle_speaker(self):
        with patch("core.playback_timing.time.monotonic", return_value=10.80), \
             patch("core.playback_timing.time.time", return_value=100.80):
            self.assertFalse(playback_idle(0.0, 100.20, 0.18))


if __name__ == "__main__":
    unittest.main()
