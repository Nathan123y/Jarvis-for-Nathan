import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from worker import intern_apply as ia
from worker import internships as wi

NOW = datetime(2026, 10, 26, 9, 0).timestamp()


def listing(**kw):
    base = {"id": "a1", "company_name": "Muon Space", "title": "Electrical Engineer Intern", "active": True,
            "is_visible": True, "terms": ["Summer 2027"], "category": "Hardware", "locations": ["San Jose, CA"],
            "degrees": ["Bachelor's"], "sponsorship": "Other", "date_posted": 100,
            "url": "https://job-boards.greenhouse.io/muonspace/jobs/5255112007"}
    base.update(kw)
    return base


class ScoutTests(unittest.TestCase):
    def test_keeps_california_summer_2027_hardware_for_bachelors(self):
        self.assertTrue(wi.wanted(listing(), {}))
        self.assertFalse(wi.wanted(listing(terms=["Summer 2026"]), {}))
        self.assertFalse(wi.wanted(listing(locations=["Austin, TX"]), {}))
        self.assertFalse(wi.wanted(listing(degrees=["Master's", "PhD"]), {}))
        self.assertFalse(wi.wanted(listing(category="Software"), {}))
        self.assertFalse(wi.wanted(listing(title="Embedded Software Engineer Intern"), {}))
        self.assertTrue(wi.wanted(listing(title="Firmware and Hardware Validation Intern"), {}))
        self.assertFalse(wi.wanted(listing(active=False), {}))

    def test_sponsorship_and_citizenship_follow_the_info_file(self):
        needs = {"will you need visa sponsorship now or in the future": "yes"}
        self.assertFalse(wi.wanted(listing(sponsorship="Does Not Offer Sponsorship"), needs))
        self.assertFalse(wi.wanted(listing(sponsorship="U.S. Citizenship is Required"), {}))
        citizen = {"citizenship (us citizen / permanent resident / other)": "US citizen"}
        self.assertTrue(wi.wanted(listing(sponsorship="U.S. Citizenship is Required"), citizen))

    def test_new_postings_are_added_once_and_closed_ones_drop_out(self):
        st = {"jobs": {}, "replies_seen": []}
        feed = [listing(), listing(id="b2", url="https://nvidia.wd5.myworkdayjobs.com/x")]
        self.assertEqual(wi.scout(st, lambda u: json.dumps(feed).encode(), {}, NOW), 2)
        self.assertEqual(st["jobs"]["a1"]["ats"], "greenhouse")
        self.assertEqual(st["jobs"]["b2"]["ats"], "workday")
        self.assertEqual(wi.scout(st, lambda u: json.dumps(feed).encode(), {}, NOW), 0)
        feed[0]["active"] = False
        wi.scout(st, lambda u: json.dumps(feed).encode(), {}, NOW)
        self.assertEqual(st["jobs"]["a1"]["status"], "closed")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.profile = self.dir / "profile.md"
        self.profile.write_text(Path(wi.PROFILE).read_text() if wi.PROFILE.exists() else "First name: Nathan\n")

    def test_blank_required_lines_and_a_missing_resume_are_gaps(self):
        self.profile.write_text("First name: Nathan\nLast name:\nGPA: 3.5\n")
        gaps = wi.profile_gaps(self.profile, self.dir / "resume.pdf")
        self.assertIn("last name", gaps)
        self.assertNotIn("first name", gaps)
        self.assertIn("resume.pdf", gaps)

    def test_unanswered_questions_are_added_once(self):
        self.profile.write_text("# Info\nFirst name: Nathan\n")
        self.assertEqual(wi.add_questions(["SpaceX Program Preference*"], self.profile), ["SpaceX Program Preference"])
        self.assertEqual(wi.add_questions(["SpaceX Program Preference"], self.profile), [])
        text = self.profile.read_text()
        self.assertEqual(text.count("SpaceX Program Preference:"), 1)
        self.assertIn("## Extra answers", text)


class ReplyTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(wi.classify("Interview invitation", "")[0], "interview")
        self.assertEqual(wi.classify("Next steps: HackerRank assessment", "")[0], "interview")
        self.assertEqual(wi.classify("Your CodeSignal assessment", "")[0], "coding test")
        self.assertEqual(wi.classify("Your application", "Unfortunately we will not be moving forward")[0], "rejection")
        self.assertEqual(wi.classify("Thank you for applying to Muon Space", "")[0], "received")
        self.assertIsNone(wi.classify("Security code for your application", "")[1])

    def test_company_match(self):
        self.assertTrue(wi.company_in("Muon Space", "Muon Space <no-reply@greenhouse.io>", "Thanks", ""))
        self.assertTrue(wi.company_in("DoorDash", "no-reply@doordash.com", "", ""))
        self.assertFalse(wi.company_in("Arc", "Marcus <m@x.com>", "Archive", ""))

    def test_replies_are_texted_once_and_update_the_job(self):
        st = {"jobs": {"a1": {"id": "a1", "company": "Muon Space", "title": "EE Intern", "status": "applied", "applied_at": NOW}},
              "replies_seen": []}
        service = MagicMock()
        api = service.users.return_value.messages.return_value
        api.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
        api.get.return_value.execute.return_value = {
            "payload": {"headers": [{"name": "From", "value": "Muon Space Recruiting <jobs@muonspace.com>"},
                                    {"name": "Subject", "value": "Interview for Electrical Engineer Intern"}]},
            "snippet": "We'd like to schedule a call"}
        sent = []
        self.assertEqual(wi.check_replies(st, service, sent.append), 1)
        self.assertEqual(wi.check_replies(st, service, sent.append), 0)
        self.assertEqual(st["jobs"]["a1"]["status"], "replied")
        self.assertIn("Muon Space wants to talk", sent[0])


class NoneAnswerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sent = []
        self.svc = wi.InternshipService(lambda m: None, lambda k, t: self.sent.append(t), state_path=self.dir / "state.json",
                                        clock=lambda: NOW)
        self.job = {"id": "q", "company": "quadric.io", "title": "DV Intern", "status": "new", "ats": "ashby", "url": "u"}

    def attempt(self, profile, questions):
        (self.dir / "profile.md").write_text(profile)
        with patch.object(wi, "PROFILE", self.dir / "profile.md"), patch.object(wi, "resume_text", return_value=""), \
                patch.object(wi, "job_description", return_value=""), patch.object(ia, "apply", side_effect=ia.NeedsInfo(questions)):
            return self.svc.apply_one({"jobs": {"q": self.job}}, self.job, False, "2026-10-26")

    def test_a_required_field_the_user_said_they_dont_have_skips_the_job_quietly(self):
        self.assertEqual(self.attempt("LinkedIn URL: none\n", ["LinkedIn Profile"]), "skipped")
        self.assertEqual(self.job["status"], "closed")
        self.assertEqual(self.sent, [])

    def test_a_blank_answer_still_asks_the_user(self):
        self.assertEqual(self.attempt("LinkedIn URL:\n", ["LinkedIn Profile"]), "needs_info")
        self.assertTrue(self.sent)

    def test_none_is_left_out_of_the_cover_letter_contact_line(self):
        self.assertEqual(ia._line("LinkedIn URL: none\n", "LinkedIn URL"), "")


class RequeueTests(unittest.TestCase):
    def test_only_the_users_own_edits_send_waiting_jobs_back_into_the_queue(self):
        d = Path(tempfile.mkdtemp())
        prof = d / "profile.md"
        prof.write_text("First name: Nathan\n")
        svc = wi.InternshipService(lambda m: None, lambda k, t: None, state_path=d / "state.json", clock=lambda: NOW)
        st = {"jobs": {"a": {"status": "needs_info"}, "b": {"status": "needs_info"}}}
        with patch.object(wi, "PROFILE", prof), patch.object(wi, "RESUME", d / "r.pdf"), patch.object(wi, "TRANSCRIPT", d / "t.pdf"):
            svc._refresh_needs_info(st)                                  # first look: nothing changed yet
            self.assertEqual(st["jobs"]["a"]["status"], "needs_info")
            wi.add_questions(["Start date year"], prof)                  # the crew adding a question
            st["info_sig"] = wi.info_sig()
            svc._refresh_needs_info(st)
            self.assertEqual(st["jobs"]["b"]["status"], "needs_info")
            prof.write_text(prof.read_text().replace("Start date year:", "Start date year: 2027"))
            svc._refresh_needs_info(st)
            self.assertEqual({j["status"] for j in st["jobs"].values()}, {"new"})


class SecurityCodeTests(unittest.TestCase):
    def test_the_code_is_read_from_greenhouses_email_not_a_nearby_word(self):
        body = ("Copy and paste this code into the security code field on your application: XJwGkSYb After you enter the "
                "code, resubmit your application.")
        api = MagicMock()
        api.list.return_value.execute.return_value = {"messages": [{"id": "m"}]}
        api.get.return_value.execute.return_value = {"internalDate": str(int(NOW * 1000)), "payload": {}, "snippet": body}
        service = MagicMock()
        service.users.return_value.messages.return_value = api
        with patch("worker.campaign.mail._text", return_value=body):
            self.assertEqual(wi.security_code(service, NOW), "XJwGkSYb")


class WorkdayTests(unittest.TestCase):
    def test_an_answer_copied_with_its_label_is_cleaned(self):
        from worker import intern_workday as wd
        self.assertEqual(wd._clean("Last name: Yousif", "Last Name"), "Yousif")
        self.assertEqual(wd._clean("Yousif", "Last Name"), "Yousif")
        self.assertEqual(wd._clean("Note: kept as written", "Essay"), "Note: kept as written")

    def test_workday_links_are_recognised(self):
        self.assertEqual(wi.ats_of("https://moog.wd5.myworkdayjobs.com/moog_external_career_site/job/Torrance-CA/x_R-1"), "workday")
        self.assertEqual(wi.ats_of("https://amazon.jobs/en/jobs/1"), "manual")


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "state.json"
        self.sent = []
        self.svc = wi.InternshipService(lambda m: None, lambda k, t: self.sent.append(t), state_path=self.path,
                                        clock=lambda: self.now)
        jobs = {str(i): {"id": str(i), "company": f"C{i}", "title": "EE Intern", "status": "new", "ats": "greenhouse",
                         "posted": i, "url": "u"} for i in range(10)}
        self.st = {"jobs": jobs, "replies_seen": []}

    def run_at(self, hh, mm, cfg):
        self.now = datetime(2026, 10, 26, hh, mm).timestamp()
        tried = []

        def fake(st, job, practice, today):
            tried.append(job["id"])
            job.update(status="applied", applied_at=self.now, tried_day=today)
            return "applied"
        with patch.object(self.svc, "apply_one", side_effect=fake):
            self.svc._maybe_apply(self.st, cfg, datetime.fromtimestamp(self.now), "2026-10-26")
        return tried

    def test_applications_are_spread_through_the_day_newest_first(self):
        cfg = dict(wi.DEFAULTS, practice=False)
        self.assertEqual(self.run_at(5, 59, cfg), [])
        self.assertEqual(self.run_at(6, 0, cfg), ["9"])
        self.assertEqual(self.run_at(6, 30, cfg), [])            # the next slot is 9:00 (15 hours / 5)
        self.assertEqual(self.run_at(9, 0, cfg), ["8"])
        for hh in (12, 15, 18):
            self.run_at(hh, 0, cfg)
        self.assertEqual(self.run_at(20, 59, cfg), [])           # five done
        self.assertEqual(sum(1 for j in self.st["jobs"].values() if j["status"] == "applied"), 5)

    def test_practice_mode_stops_after_one(self):
        cfg = dict(wi.DEFAULTS)
        self.st["practice_done"] = NOW
        self.assertEqual(self.run_at(9, 0, cfg), [])

    def test_the_directors_listings_go_first_and_skip_practice_and_pacing(self):
        cfg = dict(wi.DEFAULTS)
        self.st["practice_done"] = NOW
        self.st["jobs"]["d1"] = {"id": "d1", "company": "quadric.io", "title": "RTL", "status": "new", "ats": "ashby",
                                 "priority": True, "deadline": "2026-11-24", "url": "u"}
        self.assertEqual(self.run_at(6, 5, cfg), ["d1"])
        self.assertEqual(self.run_at(6, 10, cfg), [])            # practice still holds the regular queue


def cell(text="", link=None):
    return (text, link)


SHEET = [
    [cell("Spring 27 - Summer 27"), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell("Min GPA"),
     cell("CmpE"), cell("ME"), cell("EE")],
    [cell("Added"), cell("San Jose Area"), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell("COUNT x Major:")],
    [cell(), cell("Apply by November 24, 2026 at 9:59 PM"), cell("quadric.io"), cell("Hardware"),
     cell("RTL SoC Design Intern", "https://sjsu.joinhandshake.com/job-search/11540338?page=3"), cell("Internship - FT Sum 27"),
     cell("$50/hr"), cell("Onsite, based in Burlingame, CA"), cell(), cell(), cell(), cell("X"), cell(), cell("X")],
    [cell(), cell("Apply by November 13, 2026"), cell("Amazon"), cell("Internet"),
     cell("ASIC Engineer Intern, Summer 2027", "https://sjsu.joinhandshake.com/job-search/11664034"), cell("Internship - FT Sum 27"),
     cell(), cell(), cell(), cell(), cell(), cell("X"), cell(), cell("X")],
    [cell(), cell("Apply by October 1, 2026"), cell("Old Co"), cell(""),
     cell("Hardware Intern", "https://sjsu.joinhandshake.com/job-search/1"), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell("X")],
    [cell(), cell("Apply by December 16, 2026"), cell("TikTok Inc."), cell(""),
     cell("Backend Intern", "https://sjsu.joinhandshake.com/job-search/2"), cell(), cell(), cell(), cell(), cell(), cell(), cell("X")],
]


def boards(url):
    if "ashbyhq.com/posting-api/job-board/quadric" in url:
        return json.dumps({"jobs": [{"id": "x", "title": "RTL Intern", "jobUrl": "https://jobs.ashbyhq.com/quadric/8bc97324-bcdb-4680-a03c-4c6e590644c2"},
                                    {"id": "y", "title": "Office Coordinator", "jobUrl": "https://jobs.ashbyhq.com/quadric/y"}]}).encode()
    raise OSError("404")


class DirectorTests(unittest.TestCase):
    def test_sheet_rows_become_listings_with_the_ee_column(self):
        got = wi.sheet_listings(SHEET)
        self.assertEqual([g["company"] for g in got if g["ee"]], ["quadric.io", "Amazon", "Old Co"])
        self.assertEqual(got[0]["hs"], "11540338")
        self.assertEqual(got[0]["deadline"], "2026-11-24")

    def test_same_role(self):
        self.assertTrue(wi.same_role(wi.words("RTL SoC Design Intern"), wi.words("RTL Intern")))
        self.assertFalse(wi.same_role(wi.words("Hardware Development Engineer Intern"), wi.words("Software Development Engineer Intern")))
        self.assertTrue(wi.same_role(wi.words("(#4321158007) Hardware Engineer (Winter Co-op)"), wi.words("Hardware Engineer (Winter Co-op)")))

    def test_sync_applies_on_company_sites_and_hands_over_the_rest_once(self):
        st = {"jobs": {}, "replies_seen": []}
        auto, manual = wi.director_sync(st, SHEET, boards, "2026-10-25", NOW)
        self.assertEqual([(j["company"], j["ats"], j["priority"]) for j in auto], [("quadric.io", "ashby", True)])
        self.assertEqual([j["company"] for j in manual], ["Amazon"])        # Old Co's deadline passed; TikTok isn't for EE
        self.assertEqual(manual[0]["status"], "manual")
        self.assertIn("app.joinhandshake.com/jobs/11664034", wi.director_text(auto, manual))
        self.assertEqual(wi.director_sync(st, SHEET, boards, "2026-10-25", NOW), ([], []))

    def test_the_first_email_look_only_takes_note(self):
        service = MagicMock()
        api = service.users.return_value.messages.return_value
        api.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
        api.get.return_value.execute.return_value = {"payload": {"headers": [{"name": "Subject", "value": "Updates: Co-ops"}]}}
        st = {}
        self.assertEqual(wi.director_emails(st, service), [])
        api.list.return_value.execute.return_value = {"messages": [{"id": "m2"}, {"id": "m1"}]}
        self.assertEqual(wi.director_emails(st, service), ["Updates: Co-ops"])
        self.assertEqual(wi.director_emails(st, service), [])


class FormTests(unittest.TestCase):
    def test_pick_matches_options_loosely_but_exactly_returns_one(self):
        opts = ["I will be enrolled in a bachelor’s degree program by the start of employment", "None of the above"]
        self.assertEqual(ia.pick(opts, "I will be enrolled in a bachelor's degree program"), opts[0])
        self.assertEqual(ia.pick(["Yes", "No"], "yes"), "Yes")
        self.assertIsNone(ia.pick(["Yes", "No"], "maybe"))
        self.assertEqual(ia.pick(["United States +1", "United Kingdom +44"], "United States"), "United States +1")


if __name__ == "__main__":
    unittest.main()
