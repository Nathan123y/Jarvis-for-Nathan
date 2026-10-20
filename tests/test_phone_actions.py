"""The phone assistant's tools and call write-up, with a fake campaign, event log and Gmail."""
import asyncio
import unittest
from types import SimpleNamespace as NS

from worker.phone import actions as act, agent
from worker.phone.call import CallRecord

ROWS = [
    {"id": 7, "name": "Joe's Tacos", "category": "restaurant", "city": "San Jose", "address": "1 Main St",
     "phone": "4085550100", "email": "", "website": "", "stage": "audited", "status": "held",
     "data": {"preview_url": "https://example.github.io/joes/"}},
    {"id": 8, "name": "Joe's Tacos", "category": "restaurant", "city": "Sunnyvale", "address": "9 Elm St",
     "phone": "", "email": "joe@joestacos.com", "website": "https://joestacos.com", "stage": "sent",
     "status": "ok", "data": {}},
    {"id": 9, "name": "Rejected Place", "stage": "found", "status": "rejected", "data": {}},
]


class FakeStore:
    def __init__(self):
        self.rows = {r["id"]: dict(r, data=dict(r["data"])) for r in ROWS}
        self.suppressed, self.audits = {}, []

    def businesses(self, campaign_id, **kw):
        return list(self.rows.values())

    def business(self, bid):
        return self.rows.get(bid)

    def update_business(self, bid, *, data=None, **fields):
        self.rows[bid].update(fields)
        self.rows[bid]["data"].update(data or {})

    def suppress(self, key, reason, source=""):
        self.suppressed[key] = reason

    def is_suppressed(self, *keys):
        return next((self.suppressed[k] for k in keys if k in self.suppressed), None)

    def audit(self, campaign_id, actor, action, detail=None):
        self.audits.append((actor, action, detail))


class FakeEvents:
    def __init__(self):
        self.rows = []

    def record(self, kind, **kw):
        self.rows.append(dict(kw, kind=kind))
        return True


CONFIG = {"owner": "Nathan", "assistant": "Ava", "price": "249", "hosting": "25",
          "payment_link": "https://buy.stripe.com/test_abc", "gmail": "spam"}


class PhoneActionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.events, self.sent = FakeStore(), FakeEvents(), []
        self.actions = act.PhoneActions(self.store, self.events,
                                        lambda acct, to, subj, body: self.sent.append((acct, to, subj, body)),
                                        config=dict(CONFIG), log=lambda m: None)
        self.call = NS(directory=self.actions.directory(), record=CallRecord(caller="+16505550199"))

    def tool(self, tool_name, **args):
        return asyncio.run(self.actions(self.call, tool_name, args))

    def test_directory_skips_rejected_businesses(self):
        self.assertEqual(sorted(b.id for b in self.actions.directory().items), [7, 8])

    def test_find_business_asks_when_two_share_a_name_and_picks_by_city(self):
        self.assertIn("A few businesses match", self.tool("find_business", name="joes tacos"))
        self.assertIsNone(self.call.record.business)
        out = self.tool("find_business", name="joes tacos", city="San Jose")
        self.assertIn("Their free preview: https://example.github.io/joes/", out)
        self.assertEqual(self.call.record.business.id, 7)
        self.assertIn("isn't in Nathan's list", self.tool("find_business", name="Pizza Planet"))

    def test_send_link_emails_preview_price_and_payment_link_once_or_twice(self):
        self.tool("find_business", name="joes tacos", city="San Jose")
        self.assertIn("Sent to joe@gmail.com", self.tool("send_link", email="Joe@Gmail.com.", contact_name="Joe"))
        acct, to, subject, body = self.sent[0]
        self.assertEqual((acct, to, subject), ("spam", "joe@gmail.com", "Your website for Joe's Tacos"))
        self.assertIn("Hi Joe,", body)
        self.assertIn("https://example.github.io/joes/", body)
        self.assertIn("$249, paid once", body)
        self.assertIn("https://buy.stripe.com/test_abc", body)
        self.tool("send_link", email="joe@gmail.com")
        self.call.record.actions = [("send_link", {}, "Sent to a."), ("send_link", {}, "Sent to b.")]
        self.assertIn("Two emails already went out", self.tool("send_link", email="joe@gmail.com"))

    def test_send_link_refuses_bad_addresses_missing_link_and_opted_out(self):
        self.assertIn("doesn't look complete", self.tool("send_link", email="joe at gmail"))
        self.actions.config["payment_link"] = ""
        self.assertIn("payment link isn't set up", self.tool("send_link", email="joe@gmail.com"))
        self.actions.config["payment_link"] = "https://buy.stripe.com/x"
        self.store.suppressed["joestacos.com"] = "opted_out"
        self.tool("find_business", name="joes tacos", city="Sunnyvale")
        self.assertIn("asked not to be contacted", self.tool("send_link", email="joe@gmail.com"))
        self.assertEqual(self.sent, [])

    def test_email_failure_is_explained_to_the_ai(self):
        self.actions.send_email = lambda *a: (_ for _ in ()).throw(RuntimeError("invalid_grant"))
        self.assertIn("didn't go through", self.tool("send_link", email="joe@gmail.com"))

    def test_do_not_contact_blocks_the_campaign_from_emailing_them(self):
        self.tool("find_business", name="joes tacos", city="Sunnyvale")
        self.assertIn("won't be contacted again", self.tool("take_message", outcome="do_not_contact", summary="no"))
        self.assertEqual(set(self.store.suppressed), {"joe@joestacos.com", "joestacos.com"})
        self.assertEqual(self.store.rows[8]["status"], "suppressed")
        self.assertEqual(self.call.record.outcome, "do_not_contact")

    def test_write_up_records_one_private_event_and_the_call_on_the_business(self):
        self.tool("find_business", name="joes tacos", city="San Jose")
        rec = self.call.record
        rec.call_sid, rec.started, rec.ended, rec.ended_by = "CA9", 1000.0, 1095.0, "assistant_hung_up"
        rec.transcript = [("assistant", "Hi"), ("caller", "I want it")]
        rec.actions = [("send_link", {"email": "joe@gmail.com"}, "Sent to joe@gmail.com."),
                       ("take_message", {"outcome": "wants_site", "summary": "Wants it this week",
                                         "callback_time": "after 3pm"}, "Noted for Nathan.")]
        rec.outcome = "wants_site"
        self.actions.write_up(rec)
        ev = self.events.rows[0]
        self.assertEqual((ev["kind"], ev["source_id"], ev["status"], ev["title"], ev["private"]),
                         ("call_received", "call:CA9", "wants_site", "Joe's Tacos", True))
        self.assertEqual(ev["detail"]["emailed"], ["joe@gmail.com"])
        self.assertEqual(ev["detail"]["seconds"], 95)
        self.assertEqual(ev["detail"]["callback"], "after 3pm")
        self.assertEqual(ev["detail"]["transcript"], ["assistant: Hi", "caller: I want it"])
        self.assertEqual(self.store.rows[7]["data"]["calls"][0]["outcome"], "wants_site")
        self.assertEqual(self.store.audits[-1][1], "call")

    def test_unknown_caller_write_up_names_the_number(self):
        rec = self.call.record
        rec.ended = rec.started + 30
        self.actions.write_up(rec)
        self.assertEqual(self.events.rows[0]["title"], "A caller from +16505550199")
        self.assertEqual(self.events.rows[0]["task_id"], "")


if __name__ == "__main__":
    unittest.main()
