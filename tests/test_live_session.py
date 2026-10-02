import unittest

from core.live_session import exception_details, seconds_until_go_away, is_expected_session_expiry


class LiveSessionHelpersTests(unittest.TestCase):
    def test_go_away_duration(self):
        self.assertEqual(seconds_until_go_away("2.5s"), 2.5)
        self.assertEqual(seconds_until_go_away("0s"), 0.0)
        self.assertEqual(seconds_until_go_away(None), 10.0)

    def test_expiry_needs_go_away_or_mature_connection(self):
        self.assertTrue(is_expected_session_expiry("1008 GoAway session duration", 5))
        self.assertTrue(is_expected_session_expiry("1008 The operation was aborted", 151))
        self.assertFalse(is_expected_session_expiry("1008 The operation was aborted", 2))

    def test_nested_taskgroup_error_exposes_server_message(self):
        error = ExceptionGroup("task group", [RuntimeError("1008 GoAway connection aborted")])
        self.assertIn("GoAway", exception_details(error))


if __name__ == "__main__":
    unittest.main()
