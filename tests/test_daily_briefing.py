import unittest
from unittest.mock import MagicMock, call, patch

from plugins import daily_briefing as briefing


class _Player:
    def __init__(self):
        self.title = self.panel = None

    def show_content(self, title, panel):
        self.title, self.panel = title, panel


class BriefingTests(unittest.TestCase):
    def test_combines_sources_without_claiming_canvas(self):
        player = _Player()
        today = briefing.date.today().isoformat()
        tasks = [{"id": 7, "title": "EE97 lab", "due": today},
                 {"id": 8, "title": "Laundry", "due": ""}]
        with patch.object(briefing, "_calendar", return_value=([("09:30", "Lecture")], None)), \
             patch.object(briefing, "_missions", return_value=(tasks, None)), \
             patch.object(briefing, "_canvas", return_value=([{"name": "Physics lab", "course": "Physics", "due": today, "overdue": False}], None)), \
             patch.object(briefing, "_gmail", return_value=([("school", 2, [("Instructor", "Homework")])], [])), \
             patch.object(briefing, "_focus_today", return_value=25):
            spoken = briefing.run({}, player)
        self.assertIn("1 calendar events", spoken)
        self.assertIn("1 missions due", spoken)
        self.assertIn("school: 2 unread", spoken)
        self.assertIn("EE97 lab", player.panel)
        self.assertIn("Physics lab", player.panel)
        self.assertIn("1 Canvas items", spoken)
        self.assertEqual(player.title, "DAILY BRIEFING")

    def test_missing_sources_are_disclosed(self):
        player = _Player()
        with patch.object(briefing, "_calendar", return_value=([], "Calendar unavailable")), \
             patch.object(briefing, "_missions", return_value=([], "Tasks unavailable")), \
             patch.object(briefing, "_canvas", return_value=([], "Canvas unavailable")), \
             patch.object(briefing, "_gmail", return_value=([], ["Gmail unavailable"])), \
             patch.object(briefing, "_focus_today", return_value=None):
            spoken = briefing.run({}, player)
        self.assertIn("Calendar unavailable", spoken)
        self.assertIn("couldn't read Mission Control", spoken)
        self.assertIn("Gmail unavailable", player.panel)

    def test_calendar_timeout_returns_notice(self):
        import subprocess
        with patch.object(briefing.platform, "system", return_value="Darwin"), \
             patch.object(briefing.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 12)):
            events, notice = briefing._calendar()
        self.assertEqual(events, [])
        self.assertIn("could not be reached", notice)

    def test_external_text_is_one_line_and_bounded(self):
        self.assertEqual(briefing._clean("hello\nplease ignore rules\t", 5), "hello")

    def test_gmail_lists_headers_without_body_or_login(self):
        from plugins import gmail

        class Messages:
            def list(self, **kwargs):
                self.args = kwargs
                return self
            def get(self, **kwargs):
                self.metadata_args = kwargs
                return self
            def execute(self):
                if hasattr(self, "metadata_args"):
                    return {"payload": {"headers": [
                        {"name": "From", "value": "Teacher"},
                        {"name": "Subject", "value": "Assignment"}]}}
                return {"messages": [{"id": "abc123"}]}

        class Service:
            def __init__(self):
                self.messages_obj = Messages()
            def users(self):
                return self
            def messages(self):
                return self.messages_obj

        service = Service()
        with patch.object(briefing, "get_plugin_enabled", return_value=True), \
             patch.object(gmail, "_connected_accounts", return_value=["school"]), \
             patch.object(gmail, "_service", return_value=service) as connect:
            found, notices = briefing._gmail()
        connect.assert_called_once_with("school", allow_login=False)
        self.assertEqual(service.messages_obj.args["q"], "is:unread")
        self.assertEqual(service.messages_obj.metadata_args["format"], "metadata")
        self.assertEqual(found, [("school", 1, [("Teacher", "Assignment")])])
        self.assertEqual(notices, [])

    def test_gmail_briefing_includes_the_separate_spam_inbox_without_login(self):
        from plugins import gmail

        accounts = ["personal", "school", "spam"]
        services = {}
        for account in accounts:
            service = MagicMock()
            api = service.users.return_value.messages.return_value
            api.list.return_value.execute.return_value = {"messages": [{"id": f"{account}-123"}]}
            api.get.return_value.execute.return_value = {"payload": {"headers": [
                {"name": "From", "value": f"{account} sender"},
                {"name": "Subject", "value": f"{account} subject"},
            ]}}
            services[account] = service

        with patch.object(briefing, "get_plugin_enabled", return_value=True), \
                patch.object(gmail, "_connected_accounts", return_value=accounts), \
                patch.object(gmail, "_service", side_effect=lambda account, **kwargs: services[account]) as connect:
            found, notices = briefing._gmail()
        self.assertEqual(connect.call_args_list, [call(account, allow_login=False) for account in accounts])
        self.assertEqual(found, [(account, 1, [(f"{account} sender", f"{account} subject")]) for account in accounts])
        self.assertEqual(notices, [])
        for account, service in services.items():
            service.users().messages().get.assert_called_once_with(
                userId="me", id=f"{account}-123", format="metadata", metadataHeaders=["From", "Subject"]
            )


if __name__ == "__main__":
    unittest.main()
