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


class SubmissionStatusTests(unittest.TestCase):
    TODAY = date.today()

    def item(self, pid, name, days, subs=None, override=None):
        due = (self.TODAY + timedelta(days=days)).isoformat() + "T12:00:00"
        raw = {"plannable_id": pid, "plannable_type": "assignment", "course_id": 1,
               "context_name": "EE98", "plannable": {"name": name, "due_at": due}}
        if subs is not None:
            raw["submissions"] = subs
        if override:
            raw["planner_override"] = override
        return raw

    def statuses(self, planner, missing=()):
        pages = {"/api/v1/planner/items": list(planner),
                 "/api/v1/users/self/missing_submissions": list(missing)}
        items = canvas._status_from(lambda path, params: pages[path],
                                    self.TODAY - timedelta(days=14), self.TODAY + timedelta(days=14))
        return {i["name"]: i["status"] for i in items}

    def test_reads_canvas_submission_flags(self):
        got = self.statuses([
            self.item(1, "HW3", -2, {"submitted": True, "late": False}),
            self.item(2, "Lab 9", -1, {"submitted": True, "late": True}),
            self.item(3, "Quiz", -3, {"submitted": True, "graded": True}),
            self.item(4, "HW4", 3, {"submitted": False}),
            self.item(5, "Lab 8", -4, {"missing": True}),
            self.item(6, "Reading", -1, False),
            self.item(7, "Excused one", -1, {"excused": True}),
            self.item(8, "Self-marked", 2, False, {"marked_complete": True}),
        ])
        self.assertEqual(got, {"HW3": "submitted", "Lab 9": "submitted late", "Quiz": "submitted, graded",
                               "HW4": "not submitted yet", "Lab 8": "missing",
                               "Reading": "not submitted, past due", "Excused one": "excused",
                               "Self-marked": "marked done"})

    def test_missing_list_overrides_and_adds(self):
        got = self.statuses([self.item(1, "HW3", -2, False)],
                            [{"id": 1, "course_id": 1, "name": "HW3"},
                             {"id": 9, "course_id": 1, "name": "Old essay", "due_at": "2026-09-01T00:00:00Z"}])
        self.assertEqual(got, {"HW3": "missing", "Old essay": "missing"})

    def test_fetch_assignments_leaves_out_handed_in_work(self):
        items = [{"name": "HW3", "course": "EE", "due": self.TODAY.isoformat(), "status": "submitted", "overdue": False},
                 {"name": "HW4", "course": "EE", "due": self.TODAY.isoformat(), "status": "not submitted yet", "overdue": False},
                 {"name": "Old", "course": "EE", "due": "2026-01-01", "status": "missing", "overdue": True}]
        with patch.object(canvas, "fetch_status", return_value=(items, None, "browser")):
            todo, notice = canvas.fetch_assignments()
        self.assertIsNone(notice)
        self.assertEqual([i["name"] for i in todo], ["HW4", "Old"])

    def test_run_lists_status_for_the_voice(self):
        items = [{"name": "HW3", "course": "EE", "due": "2026-11-06", "status": "submitted", "overdue": False},
                 {"name": "HW4", "course": "EE", "due": "2026-11-12", "status": "not submitted yet", "overdue": False}]
        with patch.object(canvas, "fetch_status", return_value=(items, None, "browser")), \
             patch.object(canvas, "get_plugin_config", return_value={}):
            out = canvas.run({"action": "assignments"})
        self.assertIn("1 to do, 1 handed in", out)
        self.assertIn("submitted · 2026-11-06 · HW3", out)

    def test_session_json_prefix_is_stripped(self):
        class R:
            def text(self):
                return 'while(1);[{"id": 1}]'
        self.assertEqual(canvas._json(R()), [{"id": 1}])

    def test_browser_base_drops_a_path(self):
        self.assertEqual(canvas._browser_base("https://sjsu.instructure.com/courses"),
                         "https://sjsu.instructure.com")
        with self.assertRaises(ValueError):
            canvas._browser_base("http://127.0.0.1/x")


if __name__ == "__main__":
    unittest.main()
