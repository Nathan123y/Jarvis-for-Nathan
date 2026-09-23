import sys
import types
import unittest
from unittest.mock import patch

from plugins import chat_takeover as chat


class ChatTakeoverTests(unittest.TestCase):
    def test_focuses_messages_composer_and_pins_selected_chat(self):
        reply = types.SimpleNamespace(
            returncode=0, stderr="", stdout="Messages\tAlex\t10\t20\t900\t700\n",
        )
        with patch("subprocess.run", return_value=reply) as osascript:
            selected, region = chat._focus_messages_conversation()
            self.assertEqual(selected, "Messages / Alex")
            self.assertEqual(region, {
                "left": 10, "top": 20, "width": 900, "height": 700,
            })
            self.assertIn("set focused of composer to true", osascript.call_args.kwargs["input"])
            with self.assertRaisesRegex(RuntimeError, "conversation changed"):
                chat._focus_messages_conversation("Messages / Another chat")

    def test_replies_to_a_later_message_even_when_reply_text_repeats(self):
        class Engine:
            def __init__(self, *_):
                self.answers = iter(["sure", "STANDBY", "sure", "STANDBY"])

            def check(self, *_):
                return next(self.answers)

            def close(self):
                pass

        class Stop:
            count = 0

            def is_set(self):
                return self.count >= 4

            def wait(self, _):
                self.count += 1
                return self.is_set()

        class Player:
            def write_log(self, _):
                pass

            def show_content(self, *_):
                pass

        sent = []
        focus_calls = []

        def focus(expected=None):
            focus_calls.append(expected)
            return "Messages / Alex", {
                "left": 10, "top": 20, "width": 900, "height": 700,
            }

        with patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(FailSafeException=RuntimeError)}), \
             patch.object(chat, "_config", return_value={"gemini_api_key": "test", "os_system": "mac"}), \
             patch.object(chat, "_LiveEngine", Engine), \
             patch.object(chat, "_focus_messages_conversation", side_effect=focus), \
             patch.object(chat, "_grab_screen", side_effect=lambda region: (len(focus_calls), b"jpeg")), \
             patch.object(chat, "_screen_changed", side_effect=lambda a, b: a != b), \
             patch.object(chat, "_send_reply", side_effect=lambda *args: sent.append(args)):
            chat._state["exchanges"] = []
            chat._takeover_loop("", 60, Player(), Stop())

        self.assertEqual(sent, [("sure", "mac"), ("sure", "mac")])
        self.assertIsNone(focus_calls[0])
        self.assertTrue(all(x == "Messages / Alex" for x in focus_calls[1:]))

    def test_stop_while_model_is_working_never_sends(self):
        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _):
                return self.stopped

        stop = Stop()

        class Engine:
            def __init__(self, *_):
                pass

            def check(self, *_):
                stop.stopped = True
                return "should not send"

            def close(self):
                pass

        with patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(FailSafeException=RuntimeError)}), \
             patch.object(chat, "_config", return_value={"gemini_api_key": "test", "os_system": "mac"}), \
             patch.object(chat, "_LiveEngine", Engine), \
             patch.object(chat, "_focus_messages_conversation", return_value=(
                 "Messages / Alex", {"left": 1, "top": 1, "width": 900, "height": 700})), \
             patch.object(chat, "_grab_screen", return_value=(1, b"jpeg")), \
             patch.object(chat, "_send_reply") as send:
            chat._takeover_loop("", 60, None, stop)
            send.assert_not_called()

    def test_held_reply_retries_when_screen_has_not_changed(self):
        class Stop:
            count = 0

            def is_set(self):
                return self.count >= 2

            def wait(self, _):
                self.count += 1
                return self.is_set()

        class Engine:
            checks = 0

            def __init__(self, *_):
                pass

            def check(self, *_):
                type(self).checks += 1
                return "I'll reply"

            def close(self):
                pass

        calls = []

        def focus(expected=None):
            calls.append(expected)
            if len(calls) == 2:
                raise RuntimeError("Messages temporarily lost its composer")
            return "Messages / Alex", {
                "left": 10, "top": 20, "width": 900, "height": 700,
            }

        sent = []
        with patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(FailSafeException=RuntimeError)}), \
             patch.object(chat, "_config", return_value={"gemini_api_key": "test", "os_system": "mac"}), \
             patch.object(chat, "_LiveEngine", Engine), \
             patch.object(chat, "_focus_messages_conversation", side_effect=focus), \
             patch.object(chat, "_grab_screen", return_value=(1, b"same-screen")), \
             patch.object(chat, "_screen_changed", side_effect=lambda a, b: a != b), \
             patch.object(chat, "_send_reply", side_effect=lambda *args: sent.append(args)):
            chat._state["exchanges"] = []
            chat._takeover_loop("", 60, None, Stop())

        self.assertEqual(sent, [("I'll reply", "mac")])
        self.assertEqual(Engine.checks, 1)

    def test_other_mac_chat_apps_keep_generic_focus_path(self):
        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _):
                self.stopped = True
                return True

        class Engine:
            def __init__(self, *_):
                pass

            def check(self, *_):
                return "STANDBY"

            def close(self):
                pass

        with patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(FailSafeException=RuntimeError)}), \
             patch.object(chat, "_config", return_value={"gemini_api_key": "test", "os_system": "mac"}), \
             patch.object(chat, "_LiveEngine", Engine), \
             patch.object(chat, "_focus_messages_conversation") as focus, \
             patch.object(chat, "_grab_screen", return_value=(1, b"jpeg")):
            chat._takeover_loop("", 60, None, Stop(), "other")
            focus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
