"""Screen snips: capture, clipboard, text reading and gated sending, with macOS faked."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.macos_communications import Contact
from plugins import gmail, screen_snip


def _capture_ok(cmd, **kwargs):
    Path(cmd[-1]).write_bytes(b"\x89PNG fake")
    return MagicMock(returncode=0)


class ScreenSnipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = patch.object(screen_snip, "SNIP_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        screen_snip._last["path"] = None

    def _snip(self):
        with patch.object(screen_snip.subprocess, "run", side_effect=_capture_ok) as run, \
                patch.object(screen_snip, "_osascript") as script:
            result = screen_snip.run({"action": "snip"})
        return result, run, script

    def test_area_snip_is_saved_and_put_on_the_clipboard(self):
        result, run, script = self._snip()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["screencapture", "-x", "-i", "-s"])
        path = screen_snip._last["path"]
        self.assertEqual(path.parent, self.dir)
        self.assertEqual(script.call_args.args, (screen_snip._CLIPBOARD_IMAGE, str(path)))
        self.assertIn("clipboard", result)

    def test_cancelled_snip_leaves_nothing_to_send(self):
        with patch.object(screen_snip.subprocess, "run", return_value=MagicMock(returncode=1)):
            result = screen_snip.run({"action": "snip"})
        self.assertIn("No snip was taken", result)
        self.assertIsNone(screen_snip._last["path"])
        self.assertIn("no snip", screen_snip.run({"action": "send", "to": "Mom"}))

    def test_copy_puts_exact_text_on_the_clipboard(self):
        with patch.object(screen_snip.subprocess, "run") as run:
            result = screen_snip.run({"action": "copy", "text": "123 Main St, San Jose"})
        self.assertEqual(run.call_args.args[0], ["pbcopy"])
        self.assertEqual(run.call_args.kwargs["input"], "123 Main St, San Jose")
        self.assertIn("5 words", result)

    def test_copy_text_reads_the_last_snip_and_copies_it(self):
        self._snip()
        with patch.object(screen_snip, "_osascript", return_value="Homework due Friday") as script, \
                patch.object(screen_snip.subprocess, "run") as run:
            result = screen_snip.run({"action": "copy_text"})
        self.assertIs(script.call_args.args[0], screen_snip._OCR_SCRIPT)
        self.assertEqual(run.call_args.kwargs["input"], "Homework due Friday")
        self.assertIn("Homework due Friday", result)

    def test_group_chat_send_waits_for_confirmation_then_sends_to_that_chat(self):
        self._snip()
        deferred = {}
        with patch("core.imessage.named_groups", return_value=[("chat111", "The Boys"), ("chat222", "Family")]), \
                patch.object(screen_snip.confirm, "pending_title", return_value=""), \
                patch.object(screen_snip.confirm, "request",
                             side_effect=lambda **kw: deferred.update(kw) or "[CONFIRMATION_PENDING]"), \
                patch.object(screen_snip, "_osascript") as script:
            result = screen_snip.run({"action": "send", "to": "the boys group chat", "message": "look"})
            self.assertEqual(result, "[CONFIRMATION_PENDING]")
            script.assert_not_called()
            self.assertIn("The Boys", deferred["detail"])
            deferred["run"]()
        path = str(screen_snip._last["path"])
        self.assertEqual(script.call_args.args, (screen_snip._SEND_SCRIPT, "chat", "chat111", path, "look"))

    def test_person_send_resolves_the_contact(self):
        self._snip()
        deferred = {}
        with patch("core.imessage.named_groups", return_value=[]), \
                patch("core.macos_communications.resolve_contact",
                      return_value=Contact("Alex Kim", "+14085551212")), \
                patch.object(screen_snip.confirm, "pending_title", return_value=""), \
                patch.object(screen_snip.confirm, "request",
                             side_effect=lambda **kw: deferred.update(kw) or "[CONFIRMATION_PENDING]"), \
                patch.object(screen_snip, "_osascript") as script:
            screen_snip.run({"action": "send", "to": "Alex"})
            deferred["run"]()
        self.assertEqual(script.call_args.args[1:3], ("buddy", "+14085551212"))

    def test_ambiguous_group_name_asks_instead_of_guessing(self):
        self._snip()
        with patch("core.imessage.named_groups", return_value=[("c1", "Soccer Team"), ("c2", "Soccer Parents")]), \
                patch.object(screen_snip.confirm, "pending_title", return_value=""), \
                patch.object(screen_snip.confirm, "request") as request:
            result = screen_snip.run({"action": "send", "to": "soccer"})
        self.assertIn("More than one group chat", result)
        request.assert_not_called()

    def test_email_send_attaches_the_snip_after_confirmation(self):
        self._snip()
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "me@example.com"}
        deferred = {}
        with patch.object(gmail, "_connected_accounts", return_value=["personal"]), \
                patch.object(gmail, "_service", return_value=service), \
                patch.object(screen_snip.confirm, "pending_title", return_value=""), \
                patch.object(screen_snip.confirm, "request",
                             side_effect=lambda **kw: deferred.update(kw) or "[CONFIRMATION_PENDING]"):
            screen_snip.run({"action": "send", "to": "teacher@school.edu", "message": "Here it is"})
        service.users().messages().send.assert_not_called()
        deferred["run"]()
        raw = service.users().messages().send.call_args.kwargs["body"]["raw"]
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
        self.assertIn("To: teacher@school.edu", decoded)
        self.assertIn("Subject: Screenshot", decoded)
        self.assertIn(f'filename="{screen_snip._last["path"].name}"', decoded)
        self.assertIn("Content-Type: image/png", decoded)


if __name__ == "__main__":
    unittest.main()
