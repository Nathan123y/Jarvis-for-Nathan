import json
import unittest
from unittest.mock import patch

from core import cameras


def profiler(*devices):
    return json.dumps({"SPCameraDataType": [
        {"_name": n, "spcamera_unique-id": u, "spcamera_model-id": m} for n, u, m in devices]})


MAC = ("FaceTime HD Camera", "47B4B64B70674B9CAD2BAE273A71F4B5", "FaceTime HD Camera")
PHONE = ("Nathan's iPhone Camera", "0C3B2E1A-PHONE", "iPhone15,2")
DESK = ("Nathan's iPhone Desk View Camera", "0C3B2E1A-DESK", "iPhone15,2")


class Cameras(unittest.TestCase):
    def test_order_matches_opencv_sort_by_unique_id(self):
        devs = cameras.parse_profiler(profiler(MAC, PHONE, DESK))
        self.assertEqual([d["uid"] for d in devs], sorted([MAC[1], PHONE[1], DESK[1]]))

    def test_pick_phone_skips_desk_view_and_computer_skips_phone(self):
        devs = cameras.parse_profiler(profiler(MAC, DESK, PHONE))
        self.assertEqual(devs[cameras.pick(devs, "phone")]["name"], PHONE[0])
        self.assertEqual(devs[cameras.pick(devs, "computer")]["name"], MAC[0])

    def test_no_phone(self):
        devs = cameras.parse_profiler(profiler(MAC))
        self.assertIsNone(cameras.pick(devs, "phone"))
        self.assertEqual(cameras.pick(devs, "computer"), 0)

    def test_bad_profiler_output(self):
        self.assertEqual(cameras.parse_profiler("not json"), [])
        self.assertEqual(cameras.parse_profiler("{}"), [])

    def test_normalise_words(self):
        self.assertEqual(cameras.normalise("my iPhone"), "phone")
        self.assertEqual(cameras.normalise("phone"), "phone")
        self.assertEqual(cameras.normalise("Mac camera"), "computer")
        self.assertEqual(cameras.normalise("webcam"), "computer")
        self.assertIsNone(cameras.normalise(""))
        self.assertIsNone(cameras.normalise(None))

    def test_resolve_uses_names_then_falls_back(self):
        devs = cameras.parse_profiler(profiler(MAC, PHONE))
        with patch.object(cameras, "list_devices", return_value=devs):
            idx, used = cameras.resolve("phone", lambda: 9)
            self.assertEqual((devs[idx]["name"], used), (PHONE[0], "phone"))
        cam = ("USB Webcam", "ZZZ-USB", "UVC")
        devs3 = cameras.parse_profiler(profiler(MAC, PHONE, cam))
        with patch.object(cameras, "list_devices", return_value=devs3):
            usb = [i for i, d in enumerate(devs3) if d["name"] == "USB Webcam"][0]
            phone = [i for i, d in enumerate(devs3) if d["name"] == PHONE[0]][0]
            self.assertEqual(cameras.resolve("computer", lambda: 0, saved=usb)[0], usb)
            mac = cameras.resolve("computer", lambda: 0, saved=phone)[0]
            self.assertEqual(devs3[mac]["name"], MAC[0], "a saved index pointing at the phone is ignored")
            self.assertEqual(devs3[cameras.resolve("computer", lambda: 0, saved="junk")[0]]["name"], MAC[0])
        with patch.object(cameras, "list_devices", return_value=[]):
            self.assertEqual(cameras.resolve("computer", lambda: 3), (3, "computer"))
            self.assertEqual(cameras.resolve(None, lambda: 2), (2, "computer"))
            with self.assertRaisesRegex(RuntimeError, "iPhone camera"):
                cameras.resolve("phone", lambda: 0)


if __name__ == "__main__":
    unittest.main()
