"""The Gmail plugin's live network calls are replaced with narrow API fakes."""
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
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
            result = gmail.run({"action": "recent", "account": "personal", "count": 5, "unread_only": True})
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
            result = gmail.run({"action": "read", "account": "personal", "message_id": "mail_1234"})
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
                "action": "send", "account": "personal", "to": "friend@example.com",
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
                "action": "send", "account": "personal", "to": "Friend", "subject": "Hi", "body": "Hi",
            })
            subject = gmail.run({
                "action": "send", "account": "personal", "to": "friend@example.com",
                "subject": "Hi\nBcc: other@example.com", "body": "Hi",
            })
        self.assertIn("exact email address", recipient)
        self.assertIn("subject on one line", subject)
        service.users().messages().send.assert_not_called()

    def test_unconnected_account_does_not_start_browser_login(self):
        with patch.object(gmail, "_service", side_effect=RuntimeError("Gmail is not connected. Ask me to 'connect Gmail' first.")) as service:
            result = gmail.run({"action": "recent", "account": "school"})
        self.assertIn("connect Gmail", result)
        service.assert_called_once_with("school", allow_login=False)


    def test_two_connected_accounts_require_an_explicit_choice(self):
        with patch.object(gmail, "_connected_accounts", return_value=["personal", "school"]), \
                patch.object(gmail, "_service") as service:
            result = gmail.run({"action": "recent"})
        self.assertIn("Both Gmail accounts are connected", result)
        service.assert_not_called()

    def test_connect_requires_a_named_account(self):
        with patch.object(gmail, "_service") as service:
            result = gmail.run({"action": "connect"})
        self.assertIn("personal Gmail, school Gmail, or spam Gmail", result)
        service.assert_not_called()

    def test_single_connected_account_is_selected_automatically(self):
        service = _service_with_inbox()
        with patch.object(gmail, "_connected_accounts", return_value=["school"]), \
                patch.object(gmail, "_service", return_value=service) as open_service:
            result = gmail.run({"action": "recent", "count": 1})
        self.assertIn("Recent school Gmail", result)
        open_service.assert_called_once_with("school", allow_login=False)

    def test_three_connected_accounts_require_an_explicit_choice(self):
        with patch.object(gmail, "_connected_accounts", return_value=["personal", "school", "spam"]), \
                patch.object(gmail, "_service") as service:
            result = gmail.run({"action": "recent"})
        self.assertIn("Multiple Gmail accounts are connected", result)
        self.assertIn("personal Gmail, school Gmail, or spam Gmail", result)
        service.assert_not_called()

    def test_ambiguity_names_only_the_connected_accounts(self):
        with patch.object(gmail, "_connected_accounts", return_value=["personal", "spam"]), \
                patch.object(gmail, "_service") as service:
            result = gmail.run({"action": "recent"})
        self.assertIn("personal Gmail or spam Gmail", result)
        self.assertNotIn("school", result)
        service.assert_not_called()

    def test_spam_connection_reports_the_actual_google_address(self):
        service = _service_with_inbox()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "promotion@example.com"
        }
        with patch.object(gmail, "_service", return_value=service) as connect:
            result = gmail.run({"action": "connect", "account": "spam"})
        connect.assert_called_once_with("spam", allow_login=True)
        self.assertIn("Spam Gmail connected as promotion@example.com", result)

    def test_spam_inbox_and_read_use_the_named_account_without_login(self):
        for action, extra in [("recent", {}), ("read", {"message_id": "mail_1234"})]:
            with self.subTest(action=action):
                service = _service_with_inbox()
                with patch.object(gmail, "_service", return_value=service) as connect:
                    result = gmail.run({"action": action, "account": "spam", **extra})
                connect.assert_called_once_with("spam", allow_login=False)
                self.assertIn("spam gmail", result.lower())
                self.assertIn("Project update", result)

    def test_spam_send_reviews_the_real_sender_and_waits_for_confirmation(self):
        service = _service_with_inbox()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "promotion@example.com"
        }
        player, deferred = MagicMock(), {}

        def capture(**kwargs):
            deferred.update(kwargs)
            return "[CONFIRMATION_PENDING]"

        with patch.object(gmail, "_service", return_value=service) as connect, \
                patch.object(gmail.confirm, "pending_title", return_value=""), \
                patch.object(gmail.confirm, "request", side_effect=capture):
            result = gmail.run({
                "action": "send", "account": "spam", "to": "coach@example.com",
                "subject": "Soccer organizer", "body": "Here is the product preview.",
            }, player=player)
        connect.assert_called_once_with("spam", allow_login=False)
        self.assertEqual(result, "[CONFIRMATION_PENDING]")
        self.assertIn("Account: Spam", player.show_content.call_args.args[1])
        self.assertIn("From: promotion@example.com", deferred["detail"])
        service.users().messages().send.assert_not_called()
        deferred["run"]()
        service.users().messages().send.assert_called_once()
        sent = service.users().messages().send.call_args.kwargs
        decoded = base64.urlsafe_b64decode(sent["body"]["raw"]).decode("utf-8")
        self.assertIn("From: promotion@example.com", decoded)
        self.assertIn("To: coach@example.com", decoded)
        self.assertIn("Here is the product preview.", decoded)

    def test_named_tokens_and_legacy_personal_connection_stay_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"gmail_{name}_token.json" for name in gmail._ACCOUNTS}
            legacy = root / "gmail_token.json"
            legacy.write_text("existing personal token", encoding="utf-8")
            paths["school"].write_text("existing school token", encoding="utf-8")
            with patch.object(gmail, "_ACCOUNT_TOKENS", paths), \
                    patch.object(gmail, "_LEGACY_TOKEN", legacy):
                self.assertEqual(gmail._token_path("personal"), legacy)
                self.assertEqual(gmail._token_path("spam"), paths["spam"])
                self.assertEqual(gmail._connected_accounts(), ["personal", "school"])
                paths["spam"].write_text("new promotion token", encoding="utf-8")
                self.assertEqual(gmail._connected_accounts(), ["personal", "school", "spam"])

    def test_new_spam_sign_in_prompts_for_account_and_writes_only_its_private_token(self):
        # Replace the OAuth SDK at its import boundary; no browser or Google calls occur.
        modules = {name: ModuleType(name) for name in (
            "google.auth.transport.requests", "google.oauth2.credentials",
            "google_auth_oauthlib.flow", "googleapiclient.discovery",
        )}
        credentials_class, flow_class, build = MagicMock(), MagicMock(), MagicMock()
        modules["google.auth.transport.requests"].Request = MagicMock()
        modules["google.oauth2.credentials"].Credentials = credentials_class
        modules["google_auth_oauthlib.flow"].InstalledAppFlow = flow_class
        modules["googleapiclient.discovery"].build = build
        creds = MagicMock()
        creds.to_json.return_value = '{"token": "fake-promotion-token"}'
        flow = flow_class.from_client_secrets_file.return_value
        flow.run_local_server.return_value = creds

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"gmail_{name}_token.json" for name in gmail._ACCOUNTS}
            legacy, client = root / "gmail_token.json", root / "gmail_credentials.json"
            legacy.write_text("existing personal token", encoding="utf-8")
            paths["school"].write_text("existing school token", encoding="utf-8")
            client.write_text("fake desktop client", encoding="utf-8")
            with patch.dict(sys.modules, modules), \
                    patch.object(gmail, "_ACCOUNT_TOKENS", paths), \
                    patch.object(gmail, "_LEGACY_TOKEN", legacy), \
                    patch.object(gmail, "_CREDENTIALS", client):
                self.assertIs(gmail._service("spam", allow_login=True), build.return_value)
            flow_class.from_client_secrets_file.assert_called_once_with(str(client), gmail._SCOPES)
            flow.run_local_server.assert_called_once_with(
                host="127.0.0.1", port=0, open_browser=True, prompt="consent select_account"
            )
            credentials_class.from_authorized_user_file.assert_not_called()
            build.assert_called_once_with("gmail", "v1", credentials=creds, cache_discovery=False)
            self.assertEqual(paths["spam"].read_text(), creds.to_json.return_value)
            self.assertEqual(legacy.read_text(), "existing personal token")
            self.assertEqual(paths["school"].read_text(), "existing school token")
            self.assertFalse(paths["personal"].exists())
            self.assertFalse(paths["spam"].with_suffix(".json.tmp").exists())
            if os.name == "posix":
                self.assertEqual(paths["spam"].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
