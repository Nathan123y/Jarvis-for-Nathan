"""Reconnect labels must name a category without ever carrying error text."""
import unittest

from core.live_session import classify_disconnect, leaf_exception_name, safe_label
from core.playback_timing import parse_output_latency


class DisconnectLabelTests(unittest.TestCase):
    def test_expected_expiry_wins(self):
        self.assertEqual(classify_disconnect("ConnectionClosed", "1008 GoAway", False, True),
                         "server_expiry")

    def test_bad_api_key(self):
        self.assertEqual(classify_disconnect("APIError", "API key not valid", False, False), "api_key")

    def test_resume_rejection_only_counts_when_a_handle_was_replayed(self):
        self.assertEqual(classify_disconnect("APIError", "invalid handle", True, False), "resume_rejected")
        self.assertEqual(classify_disconnect("APIError", "invalid handle", False, False),
                         "error:apierror")

    def test_network_problems(self):
        self.assertEqual(classify_disconnect("OSError", "getaddrinfo failed", False, False), "network")

    def test_unknown_errors_are_labelled_by_type_not_message(self):
        label = classify_disconnect("ValueError", "secret text the user said", False, False)
        self.assertEqual(label, "error:valueerror")
        self.assertNotIn("secret", label)

    def test_taskgroup_errors_report_the_inner_type(self):
        group = ExceptionGroup("tg", [RuntimeError("boom")])
        self.assertEqual(leaf_exception_name(group), "RuntimeError")
        self.assertEqual(leaf_exception_name(ValueError("x")), "ValueError")

    def test_safe_label_is_short_and_plain(self):
        self.assertEqual(safe_label("no speech for 2 minutes"), "no-speech-for-2-minutes")
        self.assertEqual(safe_label("Weird <chars>!! " * 10)[:6], "weird-")
        self.assertLessEqual(len(safe_label("x" * 200)), 32)
        self.assertEqual(safe_label(None), "")


class OutputLatencySettingTests(unittest.TestCase):
    def test_unset_leaves_the_library_default_alone(self):
        for value in (None, "", "default", "auto", True, False):
            self.assertIsNone(parse_output_latency(value), value)

    def test_presets(self):
        self.assertEqual(parse_output_latency("high"), "high")
        self.assertEqual(parse_output_latency(" Low "), "low")

    def test_seconds_in_range(self):
        self.assertEqual(parse_output_latency(0.12), 0.12)
        self.assertEqual(parse_output_latency("0.2"), 0.2)

    def test_nonsense_never_raises_and_falls_back_to_default(self):
        for value in ("loud", "-1", 0.0, 5, 1e9, float("nan"), float("inf"), [], {}, object()):
            self.assertIsNone(parse_output_latency(value), value)


if __name__ == "__main__":
    unittest.main()
