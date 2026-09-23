import unittest
import sys
import types
from datetime import date, timedelta
from unittest.mock import patch

from plugins import canvas_school as canvas


class CanvasTests(unittest.TestCase):
    def test_next_month_range_includes_whole_month_and_handles_december(self):
        for today, start, end in ((date(2026, 9, 23), date(2026, 10, 1), date(2026, 10, 31)),
                                  (date(2026, 12, 23), date(2027, 1, 1), date(2027, 1, 31))):
            with patch.object(canvas, "date") as clock:
                clock.today.return_value = today
                self.assertEqual(canvas._date_range("next_month"), (start, end))

    def test_next_month_feed_reads_dates_beyond_default_two_weeks(self):
        start, end = canvas._date_range("next_month")
        day = end.strftime("%Y%m%d")
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:" + day
               + "\nSUMMARY:Final report\nEND:VEVENT\nEND:VCALENDAR\n")
        class Response:
            status_code = 200
            def iter_content(self, size):
                yield ics.encode()
            def close(self):
                pass
        requests_stub = types.ModuleType("requests")
        requests_stub.get = lambda *args, **kwargs: Response()
        with patch.dict(sys.modules, {"requests": requests_stub}), \
             patch.object(canvas, "get_plugin_config", return_value={"calendar_feed": "https://sjsu.instructure.com/feeds/calendars/private.ics"}):
            items, notice = canvas.fetch_assignments("next_month")
        self.assertIsNone(notice)
        self.assertEqual(items[0]["due"], end.isoformat())

    def test_calendar_feed_without_token_shows_dates_without_completion_claim(self):
        due = (date.today() + timedelta(days=2)).strftime("%Y%m%d")
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
               f"DTSTART;VALUE=DATE:{due}\r\nSUMMARY:Physics\\, Lab\r\n"
               "LOCATION:Physics 52\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        class Response:
            status_code = 200
            def iter_content(self, size):
                yield ics.encode()
            def close(self):
                pass
        requests_stub = types.ModuleType("requests")
        requests_stub.get = lambda *args, **kwargs: Response()
        config = {"calendar_feed": "https://sjsu.instructure.com/feeds/calendars/private.ics"}
        with patch.dict(sys.modules, {"requests": requests_stub}), \
             patch.object(canvas, "get_plugin_config", return_value=config), \
             patch.object(requests_stub, "get", return_value=Response()) as get:
            items, notice = canvas.fetch_assignments()
            reply = canvas.run({}, player=None)
        self.assertIsNone(notice)
        self.assertEqual(items[0]["name"], "Physics, Lab")
        self.assertEqual(items[0]["due"], (date.today() + timedelta(days=2)).isoformat())
        self.assertIn("submission status is unknown", reply)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_calendar_feed_rejects_other_hosts_and_plain_http(self):
        for url in ("http://sjsu.instructure.com/feeds/calendars/a.ics",
                    "https://localhost/feeds/calendars/a.ics",
                    "https://evil.com/feeds/calendars/a.ics"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                canvas._calendar_url(url)

    def test_planner_and_overdue_are_deduplicated_and_sorted(self):
        config = {"domain": "https://school.instructure.com", "token": "private-token"}
        pages = [
            [{"plannable_id": 10, "plannable_type": "assignment", "course_id": 5,
              "context_name": "EE", "plannable": {"name": "Circuit lab", "due_at": "2026-09-25T02:00:00Z"}},
             {"plannable_id": 11, "plannable_type": "assignment", "course_id": 5,
              "planner_override": {"marked_complete": True}, "plannable": {"name": "Done"}}],
            [{"id": 10, "course_id": 5, "name": "Circuit lab", "due_at": "2026-09-20T00:00:00Z",
              "course": {"name": "Electrical Engineering"}}],
        ]
        with patch.object(canvas, "get_plugin_config", return_value=config), \
             patch.object(canvas, "_request_pages", side_effect=pages) as fetch:
            items, notice = canvas.fetch_assignments()
        self.assertIsNone(notice)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["overdue"])
        self.assertEqual(fetch.call_args_list[0].args[2], "/api/v1/planner/items")
        self.assertEqual(fetch.call_args_list[1].args[2], "/api/v1/users/self/missing_submissions")

    def test_token_is_never_in_errors_or_setup_reply(self):
        with patch.object(canvas, "get_plugin_config", return_value={"domain": "school.instructure.com", "token": "PRIVATE"}), \
             patch.object(canvas, "_request_pages", side_effect=RuntimeError("PRIVATE")):
            items, notice = canvas.fetch_assignments()
        self.assertEqual(items, [])
        self.assertNotIn("PRIVATE", notice)
        self.assertNotIn("PRIVATE", canvas.run({"action": "connect"}))

    def test_domain_rejects_token_leak_destinations(self):
        for domain in ("http://school.com", "https://127.0.0.1", "https://localhost",
                       "https://school.com:8443", "https://school.com/path"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                canvas._base_url(domain)

    def test_pagination_rejects_off_site_redirect(self):
        class Response:
            status_code = 200
            links = {"next": {"url": "https://elsewhere.invalid/api/v1/planner/items"}}
            def json(self):
                return []
        requests_stub = types.ModuleType("requests")
        requests_stub.get = lambda *args, **kwargs: Response()
        with patch.dict(sys.modules, {"requests": requests_stub}), \
             patch.object(requests_stub, "get", return_value=Response()) as get:
            with self.assertRaisesRegex(ValueError, "unsafe pagination"):
                canvas._request_pages("https://school.com", "token", "/api/v1/planner/items")
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
