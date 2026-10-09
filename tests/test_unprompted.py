"""Nothing starts talking by itself while the microphone is muted."""
import unittest

from core import unprompted


class MaySpeakTests(unittest.TestCase):
    def ok(self, **kw):
        base = {"connected": True, "awake": True, "muted": False, "speaking": False}
        return unprompted.may_speak(**{**base, **kw})

    def test_speaks_when_connected_awake_unmuted_and_quiet(self):
        self.assertTrue(self.ok())

    def test_muted_is_never_spoken_into(self):
        self.assertFalse(self.ok(muted=True))
        self.assertFalse(self.ok(muted=True, speaking=False, awake=True))

    def test_asleep_disconnected_or_already_talking_all_wait(self):
        self.assertFalse(self.ok(awake=False))
        self.assertFalse(self.ok(connected=False))
        self.assertFalse(self.ok(speaking=True))

    def test_the_silence_clock_restarts_only_when_the_switch_moves(self):
        self.assertTrue(unprompted.silence_restarts(True, False))    # unmuted: the user is back
        self.assertTrue(unprompted.silence_restarts(False, True))    # muted: they stepped away
        self.assertFalse(unprompted.silence_restarts(False, False))
        self.assertFalse(unprompted.silence_restarts(True, True))


if __name__ == "__main__":
    unittest.main()
