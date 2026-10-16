import unittest
from unittest.mock import MagicMock, patch

from plugins import form_awards as fa


def form(*questions):
    return {"info": {"title": "Friendsgiving Awards"}, "items": [
        {"title": title, "questionItem": {"question": {"questionId": qid, "choiceQuestion": {
            "type": kind, "options": [{"value": o} for o in options]}}}}
        for qid, title, kind, options in questions] + [
        {"title": "Any comments?", "questionItem": {"question": {"questionId": "zz", "textQuestion": {}}}}]}


def response(email=None, when="2026-11-20T10:00:00Z", **picks):
    out = {"lastSubmittedTime": when, "answers": {
        qid: {"questionId": qid, "textAnswers": {"answers": [{"value": v} for v in values]}}
        for qid, values in picks.items()}}
    if email:
        out["respondentEmail"] = email
    return out


FORM = form(("q1", "Best Dressed", "CHECKBOX", ["Jake", "Mia", "Sam", "Ava", "Leo", "Zoe"]),
            ("q2", "Funniest", "CHECKBOX", ["Jake", "Mia"]))


class LinkTests(unittest.TestCase):
    def test_the_edit_link_or_a_bare_id_is_accepted(self):
        fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        self.assertEqual(fa.form_id(f"https://docs.google.com/forms/d/{fid}/edit?usp=sharing"), fid)
        self.assertEqual(fa.form_id(fid), fid)

    def test_the_fill_in_link_is_refused_with_what_to_do(self):
        with self.assertRaises(ValueError) as caught:
            fa.form_id("https://docs.google.com/forms/d/e/1FAIpQLSc6FlYiFjgwKoOxNRPoRpoXMiZaLUSE/viewform")
        self.assertIn("edit link", str(caught.exception))
        with self.assertRaises(ValueError):
            fa.form_id("hello")


class TallyTests(unittest.TestCase):
    def test_top_four_per_category_counting_every_ticked_box(self):
        votes = [response(q1=["Jake", "Mia"], q2=["Jake"]), response(q1=["Jake", "Sam"], q2=["Mia"]),
                 response(q1=["Mia", "Ava", "Leo"], q2=["Jake"]), response(q1=["Jake", "Ava"])]
        summary = fa.tally(FORM, votes)
        best, funny, _ = summary["categories"]
        self.assertEqual(summary["responses"], 4)
        self.assertEqual(best["title"], "Best Dressed")
        self.assertEqual(best["top"][:2], [("Jake", 3), ("Mia", 2)])
        self.assertEqual(best["top"][2], ("Ava", 2))
        self.assertEqual(funny["top"], [("Jake", 2), ("Mia", 1)])
        self.assertEqual(funny["voters"], 3)
        self.assertEqual([c["kind"] for c in summary["categories"]], ["choice", "choice", "text"])

    def test_a_tie_for_fourth_keeps_everyone_tied(self):
        votes = [response(q1=["Jake", "Mia", "Sam", "Ava", "Leo"]), response(q1=["Jake", "Mia", "Sam"]),
                 response(q1=["Jake", "Mia"]), response(q1=["Jake"])]
        top = fa.tally(FORM, votes)["categories"][0]["top"]
        self.assertEqual(top, [("Jake", 4), ("Mia", 3), ("Sam", 2), ("Ava", 1), ("Leo", 1)])
        self.assertIn("Ties for #4", fa.panel_text(fa.tally(FORM, votes), 4))
        self.assertIn("  4. Ava", fa.panel_text(fa.tally(FORM, votes), 4))

    def test_typed_other_names_are_matched_ignoring_case_and_spaces(self):
        votes = [response(q1=["jake ", "Uncle  Bob"]), response(q1=["uncle bob"]), response(q1=["Uncle Bob"])]
        top = fa.tally(FORM, votes)["categories"][0]["top"]
        self.assertEqual(top, [("Uncle Bob", 3), ("Jake", 1)])

    def test_a_resubmission_counts_once_when_emails_are_collected(self):
        votes = [response("a@x.com", "2026-11-20T10:00:00Z", q1=["Mia"]),
                 response("A@x.com", "2026-11-21T10:00:00Z", q1=["Jake"]),
                 response("b@x.com", q1=["Jake"])]
        summary = fa.tally(FORM, votes)
        self.assertEqual((summary["responses"], summary["dropped"]), (2, 1))
        self.assertEqual(summary["categories"][0]["top"], [("Jake", 2)])

    def test_no_votes_yet(self):
        summary = fa.tally(FORM, [])
        self.assertEqual(summary["categories"][0]["top"], [])
        self.assertIn("no votes yet", fa.spoken(summary, 4))


TYPED = {"info": {"title": "Awards"}, "items": [
    {"title": "Duo of the Year", "questionItem": {"question": {"questionId": "t1", "textQuestion": {}}}},
    {"title": "Picture of the Year", "questionItem": {"question": {"questionId": "f1", "fileUploadQuestion": {}}}}]}


def typed(text=None, files=()):
    answers = {}
    if text is not None:
        answers["t1"] = {"textAnswers": {"answers": [{"value": text}]}}
    if files:
        answers["f1"] = {"fileUploadAnswers": {"answers": [{"fileName": f} for f in files]}}
    return {"answers": answers}


class TypedAnswerTests(unittest.TestCase):
    def test_typed_answers_group_ignoring_case_punctuation_emoji_and_small_typos(self):
        votes = [typed("Eli and Daniella #fairs 👌🏻"), typed("eli and daniella fairs"), typed("ELI AND DANIELLA FAIRS!"),
                 typed("Michael Gabriel"), typed("micheal gabriel"), typed("Daniel and Parv"), typed("   ")]
        cat = fa.tally(TYPED, votes)["categories"][0]
        self.assertEqual(cat["kind"], "text")
        self.assertEqual(cat["top"][0][1], 3)
        self.assertEqual(cat["top"][1], ("Michael Gabriel", 2))
        self.assertEqual(cat["top"][2], ("Daniel and Parv", 1))
        self.assertEqual((cat["voters"], cat["distinct"]), (6, 3))

    def test_different_long_answers_are_not_merged(self):
        a = "going home with yasi's mom after the party at the lake house"
        b = "going home with yasi's dad after the party at the lake house"
        cat = fa.tally(TYPED, [typed(a), typed(b)])["categories"][0]
        self.assertEqual(cat["distinct"], 2)

    def test_uploads_are_listed_not_voted_on(self):
        summary = fa.tally(TYPED, [typed(files=["IMG_1 - Dani.jpeg"]), typed(files=["IMG_2 - David.png"])])
        pic = summary["categories"][1]
        self.assertEqual((pic["kind"], pic["files"]), ("file", ["IMG_1 - Dani.jpeg", "IMG_2 - David.png"]))
        self.assertIn("IMG_2 - David.png", fa.panel_text(summary, 4))
        self.assertIn("2 uploaded", fa.spoken(summary, 4))


class RunTests(unittest.TestCase):
    def test_results_reads_the_saved_form_and_shows_a_panel(self):
        service = MagicMock()
        service.forms().get().execute.return_value = FORM
        service.forms().responses().list().execute.return_value = {"responses": [response(q1=["Mia"])]}
        player = MagicMock()
        with patch.object(fa, "get_plugin_setting", return_value="https://docs.google.com/forms/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"), \
             patch.object(fa, "_service", return_value=service):
            text = fa.run({"action": "results"}, player=player)
        self.assertIn("Best Dressed: Mia 1", text)
        title, body = player.show_content.call_args[0]
        self.assertEqual(title, "FRIENDSGIVING AWARDS")
        self.assertIn("1. Mia — 1 vote", body)

    def test_without_a_form_or_a_sign_in_it_says_what_to_do(self):
        with patch.object(fa, "get_plugin_setting", return_value=""):
            self.assertIn("edit link", fa.run({}))
        with patch.object(fa, "get_plugin_setting", return_value="1AbCdEfGhIjKlMnOpQrStUvWxYz"), \
             patch.object(fa, "_service", side_effect=RuntimeError("Google Forms isn't connected yet.")):
            self.assertIn("isn't connected", fa.run({"action": "results"}))

    def test_set_form_saves_the_edit_link(self):
        with patch.object(fa, "save_plugin_config") as save:
            fa.run({"action": "set_form", "form_link": "https://docs.google.com/forms/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"})
        save.assert_called_once_with("award_form", {"form_link": "https://docs.google.com/forms/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"})


if __name__ == "__main__":
    unittest.main()
