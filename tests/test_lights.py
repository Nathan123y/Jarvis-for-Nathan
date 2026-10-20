import asyncio
import unittest
from unittest.mock import patch

from plugins import lights


def _frame(hexbytes):
    return bytes.fromhex(hexbytes)


class FakeBackend:
    def __init__(self, device="strip", answers=None, error=None, delay=0):
        self.device, self.answers, self.error, self.delay = device, answers or [], error, delay
        self.addresses, self.sent = [], []

    async def find(self, address):
        self.addresses.append(address)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.device

    async def exchange(self, device, frames, replies=False):
        self.sent.extend(frames)
        return list(self.answers)


class LightsTests(unittest.TestCase):
    def run_with(self, params, backend, settings=None):
        with patch.object(lights, "_backend", return_value=backend), \
             patch.object(lights, "get_plugin_config", return_value=settings or {}), \
             patch.object(lights, "_bluetooth_blocked", return_value=False):
            return lights.run(params)

    def test_frames_match_those_checked_on_the_strip(self):
        self.assertEqual(lights._power_frame(False), _frame("330100" + "00" * 16 + "32"))
        self.assertEqual(lights._power_frame(True), _frame("330101" + "00" * 16 + "33"))
        self.assertEqual(lights._color_frame((255, 0, 0)), _frame("330502ff0000" + "00" * 13 + "cb"))
        self.assertEqual(lights._brightness_frame(50), _frame("33047f" + "00" * 16 + "48"))
        self.assertEqual(len(lights._brightness_frame(1)), 20)
        self.assertEqual(lights._brightness_frame(1)[2], 2)
        self.assertEqual(lights._brightness_frame(100)[2], 0xFF)

    def test_on_and_off_send_one_power_frame(self):
        backend = FakeBackend()
        self.assertEqual(self.run_with({"action": "off"}, backend), "Your LED lights are off.")
        self.assertEqual(backend.sent, [lights._power_frame(False)])

    def test_color_accepts_names_hex_and_rgb(self):
        for given, rgb in (("blue", (0, 0, 255)), ("Warm White", (255, 160, 60)),
                           ("#00ff80", (0, 255, 128)), ("10, 20, 30", (10, 20, 30)),
                           ("rgb(1,2,3)", (1, 2, 3))):
            backend = FakeBackend()
            self.run_with({"action": "color", "color": given}, backend)
            self.assertEqual(backend.sent, [lights._power_frame(True), lights._color_frame(rgb)], given)

    def test_bad_color_and_brightness_send_nothing(self):
        backend = FakeBackend()
        self.assertIn("which colour", self.run_with({"action": "color", "color": "plaid"}, backend))
        self.assertIn("which colour", self.run_with({"action": "color", "color": "300,0,0"}, backend))
        for value in (0, 101, "loud", None):
            self.assertIn("between 1 and 100",
                          self.run_with({"action": "brightness", "brightness": value}, backend))
        self.assertIn("on, off, color", self.run_with({"action": "dance"}, backend))
        self.assertEqual(backend.sent, [])

    def test_brightness_turns_on_then_sets_level(self):
        backend = FakeBackend()
        self.assertIn("30 percent", self.run_with({"action": "brightness", "brightness": 30}, backend))
        self.assertEqual(backend.sent, [lights._power_frame(True), lights._brightness_frame(30)])

    def test_address_comes_only_from_settings(self):
        backend = FakeBackend()
        self.run_with({"action": "on", "address": "AA:BB"}, backend)
        self.run_with({"action": "on"}, backend, settings={"address": " 854D8E14 "})
        self.assertEqual(backend.addresses, [None, "854D8E14"])

    def test_status_reports_what_the_strip_answers(self):
        answers = [_frame("aa0101" + "00" * 16 + "aa"), _frame("aa047f" + "00" * 16 + "d1"),
                   _frame("aa0502ff000000ff" + "00" * 11 + "ad")]
        result = self.run_with({"action": "status"}, FakeBackend(answers=answers))
        self.assertEqual(result, "The LED strip is on, at 50 percent brightness, set to red.")
        self.assertIn("didn't report", self.run_with({"action": "status"}, FakeBackend()))

    def test_failures_return_plain_messages(self):
        self.assertIn("couldn't find", self.run_with({"action": "on"}, FakeBackend(device=None)))
        self.assertIn("Bluetooth is off",
                      self.run_with({"action": "on"}, FakeBackend(error=RuntimeError("Bluetooth device is turned off"))))
        self.assertIn("couldn't reach",
                      self.run_with({"action": "on"}, FakeBackend(error=OSError("secret detail"))))
        with patch.object(lights, "_TOTAL_SECONDS", 0.05):
            self.assertIn("didn't answer in time",
                          self.run_with({"action": "on"}, FakeBackend(delay=1)))

    def test_refuses_bluetooth_when_app_cannot_use_it(self):
        backend = FakeBackend()
        with patch.object(lights, "_backend", return_value=backend), \
             patch.object(lights, "_bluetooth_blocked", return_value=True):
            self.assertIn("left the lights alone", lights.run({"action": "on"}))
        self.assertEqual(backend.addresses, [])


if __name__ == "__main__":
    unittest.main()
