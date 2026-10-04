import os
import unittest
from unittest.mock import patch

from trading import credentials

PAPER_ID = "PK" + "A1B2C3D4E5F6G7H8I9"            # 20 characters, paper-style
SECRET = "x9Y8w7V6u5T4s3R2q1P0o9N8m7L6k5J4i3H2g1F0"[:40]
LIVE_ID = "AK" + "A1B2C3D4E5F6G7H8I9"


def report(key, secret, source="Jarvis Plugin Settings"):
    with patch.object(credentials, "_read", return_value=(source, key, secret)):
        return " ".join(credentials.key_report())


class KeyShapeTests(unittest.TestCase):
    def test_normal_shapes_say_so_and_point_at_regeneration(self):
        text = report(PAPER_ID, SECRET)
        self.assertIn("shapes look normal", text)
        self.assertIn("regenerated", text)

    def test_a_live_key_is_called_out(self):
        self.assertIn("LIVE-account key", report(LIVE_ID, SECRET))

    def test_swapped_boxes_are_noticed(self):
        text = report(SECRET, PAPER_ID)
        self.assertIn("does not start with PK", text)
        self.assertIn("only 20 characters", text)

    def test_a_truncated_secret_is_noticed(self):
        self.assertIn("only 25 characters", report(PAPER_ID, SECRET[:25]))

    def test_whitespace_and_identical_values_are_noticed(self):
        self.assertIn("space or a quote", report(PAPER_ID, SECRET[:20] + " " + SECRET[20:]))
        self.assertIn("identical", report(PAPER_ID, PAPER_ID))

    def test_empty_values_are_noticed(self):
        self.assertIn("empty", report("", SECRET))

    def test_the_report_never_contains_either_key(self):
        for key, secret in ((PAPER_ID, SECRET), (LIVE_ID, SECRET), (SECRET, PAPER_ID), (PAPER_ID, SECRET[:25])):
            text = report(key, secret)
            self.assertNotIn(key, text)
            self.assertNotIn(secret, text)
            self.assertNotIn(key[:6], text)
            self.assertNotIn(secret[:6], text)

    def test_environment_variables_win_and_are_named(self):
        env = {"ALPACA_PAPER_KEY_ID": PAPER_ID, "ALPACA_PAPER_SECRET_KEY": SECRET}
        with patch.dict(os.environ, env):
            self.assertEqual(credentials.load_keys(), (PAPER_ID, SECRET))
            self.assertIn("environment variables", " ".join(credentials.key_report()))


if __name__ == "__main__":
    unittest.main()
