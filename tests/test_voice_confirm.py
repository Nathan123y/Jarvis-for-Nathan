import time
import unittest

from core import confirm


class Classify(unittest.TestCase):
    def test_yes(self):
        for s in ("yes", "Yeah.", "yep send it", "go ahead", "do it", "yes please",
                  "okay send it", "sure", "Yes, Jarvis.", "send the message"):
            self.assertIs(confirm.classify(s), True, s)

    def test_no(self):
        for s in ("no", "nope", "cancel", "don't send it", "wait", "hold on",
                  "never mind", "no don't", "not yet"):
            self.assertIs(confirm.classify(s), False, s)

    def test_anything_else_is_ignored(self):
        for s in ("", "yes but change the time to five", "what did it say",
                  "send a text to mom saying hi", "it", "maybe",
                  "yes yes yes yes yes yes yes yes"):
            self.assertIsNone(confirm.classify(s), s)


class VoiceAnswer(unittest.TestCase):
    def setUp(self):
        self.ran, self.hidden = [], []
        confirm.bind(lambda t, d: None, lambda: self.hidden.append(1))
        self.addCleanup(confirm.bind, None, None)
        self.addCleanup(setattr, confirm, "_pending", None)

    def park(self):
        confirm.request("msg", "Text Mom", "hi", lambda: self.ran.append(1) or "sent")

    def wait_for_run(self):
        for _ in range(100):
            if self.ran:
                return
            time.sleep(0.01)

    def test_spoken_yes_runs_it(self):
        self.park()
        self.assertTrue(confirm.voice_answer("yeah send it", time.monotonic()))
        self.wait_for_run()
        self.assertEqual(self.ran, [1])
        self.assertEqual(confirm.pending_title(), "")

    def test_spoken_no_cancels(self):
        self.park()
        self.assertFalse(confirm.voice_answer("no wait", time.monotonic()))
        time.sleep(0.05)
        self.assertEqual(self.ran, [])
        self.assertEqual(confirm.pending_title(), "")

    def test_unclear_keeps_it_waiting(self):
        self.park()
        self.assertIsNone(confirm.voice_answer("what will it say", time.monotonic()))
        self.assertEqual(confirm.pending_title(), "Text Mom")

    def test_speech_from_before_the_request_does_not_count(self):
        started = time.monotonic()
        time.sleep(0.01)
        self.park()
        self.assertIsNone(confirm.voice_answer("yes", started))
        self.assertEqual(self.ran, [])

    def test_nothing_pending(self):
        self.assertIsNone(confirm.voice_answer("yes", time.monotonic()))

    def test_request_tells_the_model_not_to_call_again(self):
        text = confirm.request("msg", "Text Mom", "hi", lambda: "sent")
        self.assertIn("do NOT call the tool again", text)


if __name__ == "__main__":
    unittest.main()


class Outcomes(unittest.TestCase):
    def setUp(self):
        self.notes = []
        confirm.bind(lambda t, d: None, lambda: None, notify=self.notes.append)
        self.addCleanup(confirm.bind, None, None)
        self.addCleanup(setattr, confirm, "_pending", None)

    def wait(self):
        for _ in range(100):
            if self.notes:
                return
            time.sleep(0.01)

    def test_success_is_reported_back(self):
        confirm.request("m", "Email Bob", "", lambda: "Email sent to bob@x.com.")
        confirm.resolve(True)
        self.wait()
        self.assertIn("[ACTION_RESULT]", self.notes[0])
        self.assertIn("Email sent to bob@x.com.", self.notes[0])

    def test_failure_is_reported_back(self):
        def boom():
            raise RuntimeError("Gmail said no")
        confirm.request("m", "Email Bob", "", boom)
        confirm.resolve(True)
        self.wait()
        self.assertIn("FAILED", self.notes[0])
        self.assertIn("Gmail said no", self.notes[0])

    def test_cancel_is_reported_back(self):
        confirm.request("m", "Email Bob", "", lambda: "x")
        confirm.resolve(False)
        self.assertIn("Cancelled", self.notes[0])

    def test_yes_with_a_few_plain_words(self):
        for s in ("yes send it to her", "yeah go ahead and send that"):
            self.assertIs(confirm.classify(s), True, s)
        for s in ("yes but make it shorter", "yes change the subject", "yes what did it say"):
            self.assertIsNone(confirm.classify(s), s)
