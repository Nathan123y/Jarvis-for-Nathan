"""Check the native screenshot handoff without a macOS desktop in CI."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import screen_processor


class ScreenHandoffTests(unittest.TestCase):
    def test_native_error_is_returned_immediately_and_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            error = Path(temp) / "screen-00000000-0000-4000-8000-000000000001.error"
            error.write_text("ScreenCaptureKit permission denied", encoding="utf-8")
            with patch.dict(os.environ, {"JARVIS_MENU_CONTROL_DIR": temp}), \
                    patch.object(screen_processor.sys, "platform", "darwin"), \
                    patch.object(screen_processor.uuid, "uuid4", return_value="00000000-0000-4000-8000-000000000001"):
                with self.assertRaisesRegex(RuntimeError, "ScreenCaptureKit permission denied"):
                    screen_processor._capture_screen()
            self.assertFalse(error.exists())

    def test_native_response_is_read_and_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            response = Path(temp) / "screen-00000000-0000-4000-8000-000000000001.png"
            response.write_bytes(b"image-data")
            with patch.dict(os.environ, {"JARVIS_MENU_CONTROL_DIR": temp}), \
                    patch.object(screen_processor.sys, "platform", "darwin"), \
                    patch.object(screen_processor.uuid, "uuid4", return_value="00000000-0000-4000-8000-000000000001"), \
                    patch.object(screen_processor, "_compress", return_value=(b"image-data", "image/png")):
                self.assertEqual(screen_processor._capture_screen(), (b"image-data", "image/png"))
            self.assertFalse(response.exists())


if __name__ == "__main__":
    unittest.main()
