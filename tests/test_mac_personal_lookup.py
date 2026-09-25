import subprocess
import unittest
from unittest.mock import patch

from actions import calendar_events, find_contact
from core.macos_communications import Contact, ContactError


class MacPersonalLookupTests(unittest.TestCase):
    def test_find_contact_returns_masked_details_and_never_sends(self):
        with patch.object(find_contact.platform, "system", return_value="Darwin"), \
             patch.object(find_contact, "resolve_contact", return_value=Contact("Alex Smith", "+14085551234")) as lookup:
            result = find_contact.find_contact({"name": "Alex Smith"})
        lookup.assert_called_once_with("Alex Smith")
        self.assertIn("Alex Smith", result)
        self.assertIn("1234", result)
        self.assertNotIn("+14085551234", result)
        self.assertIn("No message or call", result)

    def test_find_contact_keeps_ambiguous_name_error(self):
        with patch.object(find_contact.platform, "system", return_value="Darwin"), \
             patch.object(find_contact, "resolve_contact", side_effect=ContactError("More than one match")):
            result = find_contact.find_contact({"name": "Alex"})
        self.assertIn("More than one match", result)

    def test_direct_number_is_not_misreported_as_contacts_match(self):
        with patch.object(find_contact.platform, "system", return_value="Darwin"), \
             patch.object(find_contact, "resolve_contact") as lookup:
            result = find_contact.find_contact({"name": "+14085551234"})
        self.assertIn("person's name", result)
        lookup.assert_not_called()

    def test_calendar_tomorrow_selects_next_day_and_sorts_times(self):
        def respond(argv, **kwargs):
            self.assertEqual(argv, ["osascript", "-", "1"])
            self.assertIn("on run argv", kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, "14:05\tLab\n9:07\tLecture\nAll day\tHoliday\n", "")

        with patch.object(calendar_events.platform, "system", return_value="Darwin"), \
             patch.object(calendar_events.subprocess, "run", side_effect=respond):
            result = calendar_events.calendar_events({"day": "tomorrow"})
        self.assertLess(result.index("Holiday"), result.index("Lecture"))
        self.assertLess(result.index("Lecture"), result.index("Lab"))
        self.assertIn("09:07", result)

    def test_calendar_denied_does_not_report_empty_calendar(self):
        with patch.object(calendar_events.platform, "system", return_value="Darwin"), \
             patch.object(calendar_events.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, "", "Not authorized to send Apple events (-1743)")):
            result = calendar_events.calendar_events({"day": "tomorrow"})
        self.assertIn("Automation", result)
        self.assertNotIn("No events", result)


if __name__ == "__main__":
    unittest.main()
