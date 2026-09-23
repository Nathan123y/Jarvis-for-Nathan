import unittest
from unittest.mock import Mock, patch

from core.macos_communications import (
    Contact,
    ContactError,
    is_emergency_number,
    normalize_phone,
    resolve_contact,
)


class MacCommunicationsTests(unittest.TestCase):
    def test_direct_phone_is_normalized_without_contacts_lookup(self):
        with patch("core.macos_communications._osascript") as script:
            contact = resolve_contact("+1 (408) 555-1212", phone_only=True)
        self.assertEqual(contact.handle, "+14085551212")
        script.assert_not_called()

    def test_exact_contact_result_is_returned(self):
        with patch("core.macos_communications._osascript", return_value="OK\tNathan Yousif\t+14085551212"):
            contact = resolve_contact("Nathan Yousif", phone_only=True)
        self.assertEqual(contact, Contact("Nathan Yousif", "+14085551212"))
        self.assertEqual(contact.masked_handle, "••• ••• 1212")

    def test_ambiguous_contact_is_never_silently_selected(self):
        with patch("core.macos_communications._osascript", return_value="AMBIGUOUS\tJohn A | John B"):
            with self.assertRaisesRegex(ContactError, "More than one contact"):
                resolve_contact("John", phone_only=True)

    def test_multiple_numbers_are_never_silently_selected(self):
        with patch("core.macos_communications._osascript", return_value="MULTIPLE_HANDLES\tDad"):
            with self.assertRaisesRegex(ContactError, "multiple possible numbers"):
                resolve_contact("Dad", phone_only=True)

    def test_emergency_numbers_are_blocked(self):
        self.assertTrue(is_emergency_number("911"))
        self.assertTrue(is_emergency_number("1-1-2"))
        self.assertFalse(is_emergency_number("+1 408 555 1212"))

    def test_phone_normalization_preserves_country_prefix(self):
        self.assertEqual(normalize_phone("+1 (408) 555-1212"), "+14085551212")


class ActionTests(unittest.TestCase):
    @patch("actions.send_message.confirm.request", return_value="pending")
    @patch("actions.send_message.confirm.pending_title", return_value="")
    @patch("actions.send_message.resolve_contact", return_value=Contact("Mom", "+14085551212"))
    @patch("actions.send_message._get_os", return_value="mac")
    def test_messages_uses_confirmation_without_pyautogui(self, _os, resolve, pending, request):
        from actions.send_message import send_message

        result = send_message({
            "receiver": "Mom", "message_text": "On my way", "platform": "Messages",
        })
        self.assertEqual(result, "pending")
        resolve.assert_called_once_with("Mom")
        request.assert_called_once()

    @patch("actions.phone_call.confirm.request", return_value="pending")
    @patch("actions.phone_call.confirm.pending_title", return_value="")
    @patch("actions.phone_call.resolve_contact", return_value=Contact("Dad", "+14085551212"))
    @patch("actions.phone_call._is_mac", return_value=True)
    def test_phone_call_requires_confirmation(self, _mac, resolve, pending, request):
        from actions.phone_call import phone_call

        result = phone_call({"recipient": "Dad", "mode": "phone"})
        self.assertEqual(result, "pending")
        resolve.assert_called_once_with("Dad", phone_only=True)
        request.assert_called_once()

    @patch("actions.phone_call._is_mac", return_value=True)
    def test_phone_action_refuses_direct_emergency_number(self, _mac):
        from actions.phone_call import phone_call

        result = phone_call({"recipient": "911"})
        self.assertIn("cannot place emergency calls", result)


if __name__ == "__main__":
    unittest.main()
