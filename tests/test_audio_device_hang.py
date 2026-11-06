"""A hung audio device must not take the working ones down with it.

Regression: a DisplayPort monitor whose audio was not running hung during the
startup scan. The speaker open waits on that scan, so Jarvis answered but never
spoke, even on the MacBook speakers."""
import sys
import threading
import types
import unittest
from unittest import mock

from core import audio_devices

MONITOR, SPEAKERS = 0, 1
_never = threading.Event()


class _Output:
    def __init__(self, device, hang_on, opened, **_kw):
        self.device, self.hang_on = device, hang_on
        opened.append(device)

    def start(self):
        if self.hang_on == self.device:
            _never.wait()

    def write(self, data):
        pass  # swallows audio instantly, like Windows' DirectSound fake sink

    def stop(self): pass
    def close(self): pass


class HungDeviceTests(unittest.TestCase):
    system = "Darwin"

    def setUp(self):
        self.opened = []
        for patcher in (
            mock.patch.object(audio_devices, "_OPEN_TIMEOUT", 0.2),
            mock.patch.object(audio_devices, "_RATES", {"input": 16000, "output": 24000}),
            mock.patch.object(audio_devices, "_cache", None),
            mock.patch.object(audio_devices, "_probe_results", {}),
            mock.patch.object(audio_devices, "_hung", set()),
            mock.patch.object(audio_devices, "_chosen_api", {"input": None, "output": None}),
            mock.patch("platform.system", return_value=self.system),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _use(self, hang_on=None):
        devices = [
            {"name": "P24h-10", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
            {"name": "MacBook Air Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        ]
        fake = types.SimpleNamespace(
            query_devices=lambda: devices,
            query_hostapis=lambda: [{"name": "Core Audio"}],
            RawOutputStream=lambda **kw: _Output(kw["device"], hang_on, self.opened),
        )
        patcher = mock.patch.dict(sys.modules, {"sounddevice": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolve_in_time(self, name):
        out = []
        worker = threading.Thread(
            target=lambda: out.append(audio_devices.resolve(name, "output")), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "resolve() hung on a stuck device")
        return out[0]

    def test_choosing_speakers_never_opens_the_monitor(self):
        self._use(hang_on=MONITOR)
        self.assertEqual(self._resolve_in_time("MacBook Air Speakers"), SPEAKERS)
        self.assertEqual(self.opened, [SPEAKERS])

    def test_listing_devices_opens_nothing(self):
        self._use(hang_on=MONITOR)
        self.assertEqual(audio_devices.list_devices("output"),
                         ["P24h-10", "MacBook Air Speakers"])
        self.assertEqual(self.opened, [])

    def test_saved_device_that_hangs_falls_back_to_system_default(self):
        self._use(hang_on=MONITOR)
        self.assertIsNone(self._resolve_in_time("P24h-10"))


class OtherPlatformsTests(HungDeviceTests):
    """Windows still has to measure: that is where the fake sinks live."""
    system = "Windows"
    test_choosing_speakers_never_opens_the_monitor = None
    test_listing_devices_opens_nothing = None
    test_saved_device_that_hangs_falls_back_to_system_default = None

    def test_fake_sink_is_still_measured_and_rejected(self):
        self._use()
        self.assertEqual(audio_devices.list_devices("output"), [])
        self.assertIs(audio_devices._probe_results[(None, "output")], False)


if __name__ == "__main__":
    unittest.main()
