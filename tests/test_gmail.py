"""The Gmail plugin's live network calls are replaced with narrow API fakes."""
import base64
import unittest
from unittest.mock import MagicMock, patch

from plugins import gmail


def _service_with_inbox():
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {
        "messages": [{"id": "mail_1234"}]
    }
    messages.get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "From", "value": "Sender <sender@example.com>"},
            {"name": "Subject", "value": "Project update"},
            {"name": "Date", "value": "Tue, 23 Sep 2026 10:00:00 -0700"},
        ]},
        "snippet": "Just checking in",
    }
    service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "me@example.com"}
    return service


class GmailPluginTests(unittest.TestCase):
    def test_recent_uses_inbox_and_metadata_only(self):
        service = _service_with_inbox()
        with patch.object(gmail, "_service", return_value=service):
            result = gmail.run({"action": "recent", "count": 5, "unread_only": True})
        self.assertIn("Project update", result)
        self.assertIn("mail_1234", result)
        service.users().messages().list.assert_called_once_with(
            userId="me", labelIds=["INBOX"], maxResults=5, q="is:unread"
        )
        service.users().messages().get.assert_called_once_with(
            userId="me", id="mail_1234", format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )

    def test_read_decodes_plain_text_without_marking_read(self):
        service = _service_with_inbox()
        message = service.users.return_value.messages.return_value.get.return_value.execute.return_value
        message["payload"]["mimeType"] = "text/plain"
        message["payload"]["body"] = {
            "data": base64.urlsafe_b64encode(b"Please review my document.").decode("ascii")
        }
        with patch.object(gmail, "_service", return_value=service):
            result = gmail.run({"action": "read", "message_id": "mail_1234"})
        self.assertIn("Please review my document.", result)
        service.users().messages().get.assert_called_once_with(
            userId="me", id="mail_1234", format="full"
        )

    def test_send_waits_for_human_confirmation_and_uses_the_reviewed_draft(self):
        service = _service_with_inbox()
        player = MagicMock()
        deferred = {}

        def capture(**kwargs):
            deferred.update(kwargs)
            return "[CONFIRMATION_PENDING]"

        with patch.object(gmail, "_service", return_value=service), \
                patch.object(gmail.confirm, "pending_title", return_value=""), \
                patch.object(gmail.confirm, "request", side_effect=capture):
            result = gmail.run({
                "action": "send", "to": "friend@example.com",
                "subject": "Hello", "body": "See you soon!",
            }, player=player)

        self.assertEqual(result, "[CONFIRMATION_PENDING]")
        service.users().messages().send.assert_not_called()
        self.assertIn("See you soon!", player.show_content.call_args.args[1])
        self.assertIn("friend@example.com", deferred["detail"])
        service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent_456"}
        deferred["run"]()  # This normally runs only when core.confirm receives a HUD click.
        service.users().messages().send.assert_called_once()
        sent = service.users().messages().send.call_args.kwargs
        decoded = base64.urlsafe_b64decode(sent["body"]["raw"]).decode("utf-8")
        self.assertIn("To: friend@example.com", decoded)
        self.assertIn("Subject: Hello", decoded)
        self.assertIn("See you soon!", decoded)

    def test_rejects_ambiguous_recipient_and_header_injection(self):
        service = _service_with_inbox()
        with patch.object(gmail, "_service", return_value=service):
            recipient = gmail.run({
                "action": "send", "to": "Friend", "subject": "Hi", "body": "Hi",
            })
            subject = gmail.run({
                "action": "send", "to": "friend@example.com",
                "subject": "Hi\nBcc: other@example.com", "body": "Hi",
            })
        self.assertIn("exact email address", recipient)
        self.assertIn("subject on one line", subject)
        service.users().messages().send.assert_not_called()

    def test_unconnected_account_does_not_start_browser_login(self):
        with patch.object(gmail, "_service", side_effect=RuntimeError("Gmail is not connected. Ask me to 'connect Gmail' first.")) as service:
            result = gmail.run({"action": "recent"})
        self.assertIn("connect Gmail", result)
        service.assert_called_once_with(allow_login=False)


if __name__ == "__main__":
    unittest.main()
