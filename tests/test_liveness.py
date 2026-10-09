"""A live connection that stops answering has to be noticed and rebuilt."""
import unittest

from core import liveness


class LivenessTests(unittest.TestCase):
    def ask(self, **kw):
        base = {"now": 1000.0, "last_server_msg": 990.0, "last_mic_send": 999.0}
        return liveness.should_reconnect(**{**base, **kw})

    def test_a_healthy_session_is_left_alone(self):
        self.assertFalse(self.ask())

    def test_streaming_audio_into_a_silent_connection_is_rebuilt(self):
        self.assertTrue(self.ask(last_server_msg=1000.0 - liveness.QUIET_LIMIT_S - 1))

    def test_a_quiet_session_we_are_not_feeding_is_not_a_fault(self):
        # Muted, asleep or push-to-talk: no audio goes out, so nothing is expected back.
        self.assertFalse(self.ask(last_server_msg=1.0, last_mic_send=1000.0 - liveness.MIC_RECENT_S - 1))

    def test_nothing_is_judged_before_there_is_anything_to_compare(self):
        self.assertFalse(self.ask(last_server_msg=0.0))
        self.assertFalse(self.ask(last_mic_send=0.0))

    def test_the_window_is_generous_enough_not_to_interrupt_a_pause(self):
        self.assertGreaterEqual(liveness.QUIET_LIMIT_S, 120.0)
        self.assertFalse(self.ask(last_server_msg=1000.0 - liveness.QUIET_LIMIT_S + 5))


if __name__ == "__main__":
    unittest.main()
