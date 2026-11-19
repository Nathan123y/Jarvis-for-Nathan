"""Collab Studio: the customer journey and the rules that protect it (privacy, versions, approvals, payment, production)."""
import hashlib
import hmac
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

from collab_studio import design
from collab_studio.config import load_env
from collab_studio.payments import StripePayments
from collab_studio.services import Studio, UserError
from collab_studio.web import create_app


def png(color=(200, 30, 40), size=(800, 1100)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


FORM = {"idea": "FC Barcelona × Dallas Cowboys", "garment": "soccer", "style": "vintage_waves",
        "colors": "garnet, navy, silver", "back_name": "Yousif", "back_number": "10", "instructions": "",
        "customer_name": "Sam Rivera", "email": "sam@example.com"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("STRIPE_SECRET_KEY", None)
        with mock.patch.dict(os.environ, {"COLLAB_ADMIN_PASSWORD": "pw-test", "COLLAB_SECRET_KEY": "s" * 32,
                                          "COLLAB_IMAGE_PROVIDER": "demo", "COLLAB_PAYMENT_PROVIDER": "simulated",
                                          "COLLAB_EMAIL_PROVIDER": "preview"}):
            self.env = load_env(self.tmp.name)
        self.studio = self.make_studio()
        self.client = TestClient(create_app(self.env, self.studio, start_worker=False))

    def make_studio(self, **kw):
        return Studio(self.env, **kw)

    def tearDown(self):
        self.client.close()
        self.studio.db.close_all()
        self.tmp.cleanup()

    # helpers
    def submit(self, client=None, **over):
        c = client or self.client
        r = c.post("/create", data={**FORM, **over}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text[:500])
        token = r.headers["location"].split("/p/")[1]
        return token, self.studio.by_token(token)

    def admin(self):
        r = self.client.post("/admin/login", data={"password": "pw-test"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def csrf(self, path):
        html = self.client.get(path).text
        return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)

    def designed(self, **over):
        token, req = self.submit(**over)
        self.studio.process_pending()
        vs = self.studio.versions(req["id"])
        self.studio.send_versions(req["id"], [v["id"] for v in vs])
        return token, req, self.studio.versions(req["id"])

    def approved(self, **over):
        token, req, vs = self.designed(**over)
        self.client.post(f"/p/{token}/choose", data={"version_id": vs[0]["id"]})
        proof = self.studio.current_proof(req["id"])
        r = self.client.post(f"/p/{token}/approve", data={"version_id": vs[0]["id"], "proof_id": proof["id"],
                                                          "back_name": "YOUSIF", "back_number": "10", "size": "L",
                                                          "quantity": "2", "confirm": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return token, req, vs

    def paid(self, **over):
        token, req, vs = self.approved(**over)
        r = self.client.post(f"/p/{token}/checkout", follow_redirects=False)
        sid = r.headers["location"].split("session=")[1]
        r = self.client.post(f"/p/{token}/test-checkout", data={"session": sid, "first_name": "Sam", "last_name": "R",
                                                                 "address1": "1 Main", "city": "X", "zip": "1",
                                                                 "country": "US"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return token, req, vs, sid


class JourneyTest(Base):
    def test_full_customer_journey_to_shipped(self):
        self.assertIn("Your collab.", self.client.get("/").text)
        self.assertIn("Create My Design", self.client.get("/").text)
        token, req = self.submit()
        page = self.client.get(f"/p/{token}").text
        self.assertIn("Designing your concepts", page)
        self.assertEqual(self.studio.order(req["id"]), None)
        self.studio.process_pending()
        vs = self.studio.versions(req["id"])
        self.assertEqual([v["label"] for v in vs], ["A", "B"])
        self.assertTrue(all(v["state"] == "draft" for v in vs))               # admin reviews first
        self.assertNotIn("Design A", self.client.get(f"/p/{token}").text)
        self.admin()
        tok = self.csrf(f"/admin/r/{req['id']}")
        self.client.post(f"/admin/r/{req['id']}/send", data={"csrf": tok, "version_ids": [vs[0]["id"], vs[1]["id"]]})
        page = self.client.get(f"/p/{token}").text
        self.assertIn("Choose This Design", page)
        self.assertIn("not generated from your request", page)                # demo is labelled
        token, req, vs, sid = None, req, vs, None
        # choose, approve, pay
        tkn = self._token(req)
        self.client.post(f"/p/{tkn}/choose", data={"version_id": vs[1]["id"]})
        proof = self.studio.current_proof(req["id"])
        self.assertEqual(proof["version_id"], vs[1]["id"])
        self.client.post(f"/p/{tkn}/approve", data={"version_id": vs[1]["id"], "proof_id": proof["id"],
                                                    "back_name": "YOUSIF", "back_number": "10", "size": "M",
                                                    "quantity": "1", "confirm": "1"})
        o = self.studio.order(req["id"])
        self.assertEqual(o["total_cents"], 6400 + 800)
        self.assertIn("Demo price", self.client.get(f"/p/{tkn}/approve").text)
        r = self.client.post(f"/p/{tkn}/checkout", follow_redirects=False)
        self.assertIn("/test-checkout?session=", r.headers["location"])
        sid = r.headers["location"].split("session=")[1]
        self.assertIn("TEST MODE", self.client.get(f"/p/{tkn}/test-checkout?session={sid}").text)
        self.client.post(f"/p/{tkn}/test-checkout", data={"session": sid, "first_name": "Sam", "address1": "1 Main",
                                                          "city": "X", "zip": "1", "country": "US"})
        self.assertEqual(self.studio.get_request(req["id"])["status"], "preparing_production")
        self.assertEqual(self.studio.order(req["id"])["paid_approval_id"], self.studio.current_approval(req["id"])["id"])
        # production: branding + artwork, then submit and ship
        rid = req["id"]
        self.studio.set_branding(rid, "cleared", "original art only")
        for p in ("front", "back", "right_sleeve", "left_sleeve", "collar"):
            self.studio.upload_artwork(rid, p, png())
        self.studio.set_artwork_review(rid, True)
        self.assertEqual(self.studio.production_blockers(rid), [])
        self.studio.submit_to_production(rid, "MFG-1")
        self.studio.add_tracking(rid, "UPS", "1Z999", "https://example.com/t/1Z999")
        page = self.client.get(f"/p/{tkn}/order").text
        self.assertIn("Shipped", page)
        self.assertIn("1Z999", page)
        kinds = [e["kind"] for e in self.studio.db.all("SELECT kind FROM emails WHERE request_id=? ORDER BY id", (rid,))]
        self.assertEqual(kinds, ["request_received", "designs_ready", "payment_confirmed", "order_shipped"])

    def _token(self, req):
        return self.studio.token_for(self.studio.get_request(req["id"]))

    def test_request_survives_refresh_and_restart(self):
        token, req = self.submit()
        self.assertIn(FORM["idea"], self.client.get(f"/p/{token}").text)
        studio2 = Studio(self.env)                                            # fresh process, same data folder
        client2 = TestClient(create_app(self.env, studio2, start_worker=False))
        page = client2.get(f"/p/{token}").text
        self.assertIn(FORM["idea"], page)
        self.assertIn("YOUSIF", page)
        self.assertEqual(studio2.process_pending(), 1)                       # the queued job survived too
        client2.close()
        studio2.db.close_all()

    def test_builder_validates_and_keeps_values(self):
        r = self.client.post("/create", data={**FORM, "email": "nope", "back_number": "123"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("valid email", r.text)
        self.assertIn("0–99", r.text)
        self.assertIn(FORM["idea"].replace("×", "×"), r.text)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM requests")["n"], 0)

    def test_reference_uploads_are_validated(self):
        r = self.client.post("/create", data=FORM, files=[("references", ("x.png", b"not an image", "image/png"))])
        self.assertEqual(r.status_code, 422)
        self.assertIn("readable PNG, JPEG or WebP", r.text)
        r = self.client.post("/create", data=FORM, files=[("references", ("ok.png", png(), "image/png"))],
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        rid = self.studio.db.one("SELECT id FROM requests")["id"]
        self.assertEqual(len(self.studio.storage.list(rid, "reference")), 1)

    def test_third_party_branding_needs_review(self):
        _, req = self.submit()
        self.assertEqual(req["branding_status"], "pending")
        b = self.studio.latest_brief(req["id"])["data"]
        self.assertTrue(b["third_party_branding"])
        self.assertEqual(b["front_text"], "")                     # no team names on the garment by default
        self.assertIn("no official team logos", design.concept_prompt(b, b["directions"][0], "front").lower())
        _, own = self.submit(idea="Rivera Plumbing FC", own_brand="1")
        self.assertEqual(own["branding_status"], "not_needed")


class PrivacyTest(Base):
    def test_customers_cannot_see_each_other(self):
        ta, a, va = self.designed()
        tb, b, vb = self.designed(email="other@example.com", idea="Kobe × Ronaldinho")
        self.assertNotEqual(ta, tb)
        self.assertEqual(self.client.get(f"/p/{ta}/asset/{va[0]['front_asset_id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/p/{ta}/asset/{vb[0]['front_asset_id']}").status_code, 404)
        self.assertNotIn("Kobe", self.client.get(f"/p/{ta}").text)
        # choosing someone else's version through my link does nothing
        r = self.client.post(f"/p/{ta}/choose", data={"version_id": vb[0]["id"]}, follow_redirects=False)
        self.assertIsNone(self.studio.get_request(b["id"])["chosen_version_id"])
        self.assertIsNone(self.studio.get_request(a["id"])["chosen_version_id"])
        # guessed and malformed links
        for bad in ("abc", ta[:-1] + ("x" if ta[-1] != "x" else "y"), "0" * 43):
            self.assertEqual(self.client.get(f"/p/{bad}").status_code, 404)
            self.assertEqual(self.client.get(f"/p/{bad}/status.json").status_code, 404)

    def test_drafts_and_production_files_are_not_customer_visible(self):
        token, req = self.submit()
        self.studio.process_pending()
        draft = self.studio.versions(req["id"])[0]
        self.assertEqual(self.client.get(f"/p/{token}/asset/{draft['front_asset_id']}").status_code, 404)
        art = self.studio.storage.save(req["id"], "production", png(), panel="front")
        self.assertEqual(self.client.get(f"/p/{token}/asset/{art}").status_code, 404)

    def test_token_is_not_stored_in_plain_text(self):
        token, req = self.submit()
        row = self.studio.db.one("SELECT * FROM requests WHERE id=?", (req["id"],))
        self.assertNotIn(token, json.dumps(row))

    def test_private_pages_are_not_cached_or_indexed(self):
        token, _ = self.submit()
        r = self.client.get(f"/p/{token}")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("noindex", r.headers["x-robots-tag"])
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")

    def test_rotated_link_replaces_old(self):
        token, req = self.submit()
        self.studio.rotate_link(req["id"])
        self.assertEqual(self.client.get(f"/p/{token}").status_code, 404)
        new = self.studio.token_for(self.studio.get_request(req["id"]))
        self.assertEqual(self.client.get(f"/p/{new}").status_code, 200)


class AdminSecurityTest(Base):
    def test_admin_requires_login_and_csrf(self):
        _, req = self.submit()
        self.assertEqual(self.client.get("/admin", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/admin/settings", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/admin/asset/1").status_code, 401)
        self.assertEqual(self.client.post("/admin/login", data={"password": "wrong"}).status_code, 401)
        self.admin()
        self.assertEqual(self.client.get("/admin").status_code, 200)
        r = self.client.post(f"/admin/r/{req['id']}/grant-revision", data={})
        self.assertEqual(r.status_code, 403)                                    # no CSRF token
        tok = self.csrf(f"/admin/r/{req['id']}")
        self.client.post(f"/admin/r/{req['id']}/grant-revision", data={"csrf": tok})
        self.assertEqual(self.studio.get_request(req["id"])["revision_limit"], 2)

    def test_cross_site_posts_are_refused(self):
        token, _ = self.submit()
        r = self.client.post(f"/p/{token}/checkout", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.client.post("/admin/login", data={"password": "x"})
        self.assertEqual(self.client.post("/admin/login", data={"password": "pw-test"}).status_code, 429)

    def test_settings_are_editable(self):
        self.admin()
        st = self.studio.settings()
        tok = self.csrf("/admin/settings")
        g = st["garments"]
        g["basketball"]["price"] = 71.5
        data = {"csrf": tok, "business_name": "Crossover Lab", "support_email": "a@b.co", "currency": "usd",
                "revision_limit": "2", "max_generations_per_request": "4", "generation_max_attempts": "3",
                "tax_rate_percent": "8.25", "tax_note": "x", "turnaround_design": "x", "turnaround_production": "x",
                "prices_are_demo": "1", "require_admin_review": "1", "garments": json.dumps(g),
                "shop_links": json.dumps(st["shop_links"]), "faq": json.dumps(st["faq"]), "ship_countries": '["US"]'}
        self.client.post("/admin/settings", data=data)
        st = self.studio.settings()
        self.assertEqual(st["business_name"], "Crossover Lab")
        self.assertEqual(st["revision_limit"], 2)
        self.assertEqual(st["garments"]["basketball"]["price"], 71.5)
        self.assertIn("Crossover Lab", self.client.get("/").text)
        q = self.studio.quote("basketball", 1)
        self.assertEqual(q["tax_cents"], round((7150 + 800) * 0.0825))


class RevisionTest(Base):
    def test_revision_preserves_history(self):
        token, req, vs = self.designed()
        before = {v["id"]: (v["front_asset_id"], v["back_asset_id"], v["prompt"]) for v in vs}
        r = self.client.post(f"/p/{token}/revise", data={"version_id": vs[0]["id"], "note": "Swap navy for black"})
        self.assertEqual(self.studio.get_request(req["id"])["status"], "revision_in_progress")
        self.studio.process_pending()
        all_v = self.studio.versions(req["id"])
        self.assertEqual([v["version_no"] for v in all_v], [1, 2, 3])
        new = all_v[-1]
        self.assertEqual(new["label"], "A2")
        self.assertEqual(new["parent_id"], vs[0]["id"])
        for v in all_v[:2]:
            self.assertEqual((v["front_asset_id"], v["back_asset_id"], v["prompt"]), before[v["id"]])
        self.assertIn("Swap navy for black", new["prompt"])
        self.assertIn("Keep from the chosen design", new["prompt"])
        self.studio.send_versions(req["id"], [new["id"]])
        page = self.client.get(f"/p/{token}").text
        self.assertIn("Previous versions (2)", page)
        self.assertIn("Swap navy for black", page)
        self.assertEqual(len(self.studio.db.all("SELECT * FROM briefs WHERE request_id=?", (req["id"],))), 2)
        emails = [e["kind"] for e in self.studio.db.all("SELECT kind FROM emails WHERE request_id=?", (req["id"],))]
        self.assertIn("revision_ready", emails)

    def test_revision_limit(self):
        token, req, vs = self.designed()
        self.studio.request_revision(req["id"], vs[0]["id"], "Bigger numbers please")
        self.studio.process_pending()
        with self.assertRaises(UserError):
            self.studio.request_revision(req["id"], vs[0]["id"], "One more change")
        self.studio.grant_revision(req["id"])
        self.studio.request_revision(req["id"], vs[0]["id"], "One more change")


class ApprovalTest(Base):
    def test_approval_records_exact_version_and_proof(self):
        token, req, vs = self.approved()
        a = self.studio.current_approval(req["id"])
        self.assertEqual(a["version_id"], vs[0]["id"])
        self.assertEqual(a["proof_id"], self.studio.current_proof(req["id"])["id"])
        self.assertEqual((a["size"], a["quantity"], a["back_name"], a["back_number"]), ("L", 2, "YOUSIF", "10"))
        self.assertEqual(self.studio.order(req["id"])["approval_id"], a["id"])

    def test_stale_proof_cannot_be_approved(self):
        token, req, vs = self.designed()
        self.studio.choose(req["id"], vs[0]["id"])
        old = self.studio.current_proof(req["id"])
        self.studio.choose(req["id"], vs[1]["id"])                     # customer switches design in another tab
        with self.assertRaises(UserError):
            self.studio.approve(req["id"], vs[0]["id"], old["id"], "YOUSIF", "10", "L", 1, True)
        self.assertIsNone(self.studio.current_approval(req["id"]))

    def test_changes_invalidate_approval(self):
        token, req, vs = self.approved()
        rid = req["id"]
        a1 = self.studio.current_approval(rid)
        self.studio.choose(rid, vs[1]["id"])                            # different design
        self.assertIsNone(self.studio.current_approval(rid))
        p = self.studio.current_proof(rid)
        self.studio.approve(rid, vs[1]["id"], p["id"], "YOUSIF", "10", "M", 1, True)
        self.studio.upload_proof(rid, png(), png((10, 10, 10)))        # new proof
        self.assertIsNone(self.studio.current_approval(rid))
        self.assertEqual(self.studio.get_request(rid)["status"], "final_proof_ready")
        p = self.studio.current_proof(rid)
        self.studio.approve(rid, vs[1]["id"], p["id"], "YOUSIF", "10", "M", 1, True)
        self.studio.set_personalisation(rid, "RIVERA", "7")             # admin edits personalisation
        self.assertIsNone(self.studio.current_approval(rid))
        self.assertIsNone(self.studio.current_proof(rid))               # old proof shows the old name
        self.assertNotEqual(a1["fingerprint"], "")

    def test_customer_name_change_needs_new_proof(self):
        token, req, vs = self.designed()
        self.studio.choose(req["id"], vs[0]["id"])
        p = self.studio.current_proof(req["id"])
        r = self.client.post(f"/p/{token}/approve", data={"version_id": vs[0]["id"], "proof_id": p["id"],
                                                          "back_name": "RIVERA", "back_number": "7", "size": "L",
                                                          "quantity": "1", "confirm": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(self.studio.current_approval(req["id"]))
        self.assertIsNone(self.studio.current_proof(req["id"]))
        self.assertEqual(self.studio.get_request(req["id"])["back_name"], "RIVERA")

    def test_approval_requires_confirmation_and_valid_size(self):
        token, req, vs = self.designed()
        self.studio.choose(req["id"], vs[0]["id"])
        p = self.studio.current_proof(req["id"])
        with self.assertRaises(UserError):
            self.studio.approve(req["id"], vs[0]["id"], p["id"], "YOUSIF", "10", "L", 1, False)
        with self.assertRaises(UserError):
            self.studio.approve(req["id"], vs[0]["id"], p["id"], "YOUSIF", "10", "XXXXL", 1, True)
        with self.assertRaises(UserError):
            self.studio.approve(req["id"], vs[0]["id"], p["id"], "YOUSIF", "10", "L", 0, True)

    def test_paid_design_is_locked_for_customer(self):
        token, req, vs, _ = self.paid()
        with self.assertRaises(UserError):
            self.studio.choose(req["id"], vs[1]["id"])
        with self.assertRaises(UserError):
            self.studio.request_revision(req["id"], vs[0]["id"], "change it please")


class ProductionGateTest(Base):
    def ready(self, rid, garment="soccer"):
        self.studio.set_branding(rid, "cleared")
        for p in [x["name"] for x in self.studio.settings()["garments"][garment]["panels"]]:
            self.studio.upload_artwork(rid, p, png())
        self.studio.set_artwork_review(rid, True)

    def test_unpaid_order_cannot_enter_production(self):
        token, req, vs = self.approved()
        self.ready(req["id"])
        self.assertIn("Payment isn't confirmed.", self.studio.production_blockers(req["id"]))
        with self.assertRaises(UserError):
            self.studio.submit_to_production(req["id"])
        self.assertEqual(self.studio.order(req["id"])["production_status"], "not_submitted")

    def test_unapproved_request_cannot_enter_production(self):
        token, req, vs = self.designed()
        with self.assertRaises(UserError):
            self.studio.submit_to_production(req["id"])
        self.assertIsNone(self.studio.order(req["id"]))

    def test_each_gate_blocks(self):
        token, req, vs, _ = self.paid()
        rid = req["id"]
        b = self.studio.production_blockers(rid)
        self.assertIn("Production artwork hasn't passed review.", b)
        self.assertIn("Branding permission is unresolved.", b)
        self.studio.set_branding(rid, "declined")
        self.assertIn("Branding permission was declined.", self.studio.production_blockers(rid))
        self.ready(rid)
        self.assertEqual(self.studio.production_blockers(rid), [])
        # admin uploads a new proof after payment: needs a fresh approval before production
        self.studio.upload_proof(rid, png(), png())
        b = self.studio.production_blockers(rid)
        self.assertIn("There's no valid customer approval of the final proof.", b)
        with self.assertRaises(UserError):
            self.studio.submit_to_production(rid)

    def test_artwork_must_pass_checks(self):
        token, req, vs, _ = self.paid()
        rid = req["id"]
        with self.assertRaises(UserError):
            self.studio.set_artwork_review(rid, True)                     # panels missing
        st = self.studio.settings()
        st["garments"]["soccer"]["panels"][0].update({"min_width": 3000, "min_height": 3000})
        self.studio.update_settings({"garments": st["garments"]})
        for p in ("front", "back", "right_sleeve", "left_sleeve", "collar"):
            self.studio.upload_artwork(rid, p, png())
        rep = dict((p, s) for p, s, _ in self.studio.validate_artwork(rid))
        self.assertEqual(rep["front"], "fail")
        self.assertEqual(rep["back"], "review")
        with self.assertRaises(UserError):
            self.studio.set_artwork_review(rid, True)
        # concept and production assets stay separate
        kinds = {a["kind"] for a in self.studio.db.all("SELECT kind FROM assets WHERE request_id=?", (rid,))}
        self.assertTrue({"concept", "proof", "production"} <= kinds or {"concept", "production"} <= kinds)

    def test_production_submission_is_once_only(self):
        token, req, vs, _ = self.paid()
        self.ready(req["id"])
        self.studio.submit_to_production(req["id"])
        with self.assertRaises(UserError):
            self.studio.submit_to_production(req["id"])

    def test_admin_submit_button_respects_gates(self):
        token, req, vs = self.approved()
        self.admin()
        tok = self.csrf(f"/admin/r/{req['id']}")
        r = self.client.post(f"/admin/r/{req['id']}/submit", data={"csrf": tok}, follow_redirects=True)
        self.assertIn("Can&#x27;t submit yet", r.text)
        self.assertEqual(self.studio.order(req["id"])["production_status"], "not_submitted")


class PaymentIdempotencyTest(Base):
    def test_duplicate_confirmations_do_not_duplicate(self):
        token, req, vs, sid = self.paid()
        self.assertEqual(self.studio.confirm_checkout(sid), "duplicate")
        r = self.client.post(f"/p/{token}/test-checkout", data={"session": sid}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM payment_events WHERE outcome='paid'")["n"], 1)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM emails WHERE kind='payment_confirmed'")["n"], 1)

    def test_reapproval_updates_the_one_order(self):
        token, req, vs = self.approved()
        p = self.studio.current_proof(req["id"])
        self.studio.approve(req["id"], vs[0]["id"], p["id"], "YOUSIF", "10", "XL", 3, True)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)
        o = self.studio.order(req["id"])
        self.assertEqual(o["quantity"], 3)
        self.assertEqual(o["total_cents"], 6400 * 3 + 800)

    def test_old_checkout_after_reapproval_cannot_pay_the_new_approval(self):
        token, req, vs = self.approved()
        url = self.studio.start_checkout(req["id"])
        old_sid = url.split("session=")[1]
        p = self.studio.current_proof(req["id"])
        self.studio.approve(req["id"], vs[0]["id"], p["id"], "YOUSIF", "10", "XL", 3, True)
        self.assertEqual(self.studio.confirm_checkout(old_sid), "paid")    # money taken for the old approval
        b = self.studio.production_blockers(req["id"])
        self.assertTrue(any("paid for a different approval" in x for x in b))

    def test_unknown_or_foreign_session_is_refused(self):
        ta, a, _ = self.approved()
        self.assertEqual(self.studio.confirm_checkout("sim_doesnotexist"), "unknown")
        tb, b, _ = self.approved(email="b@example.com")
        url = self.studio.start_checkout(b["id"])
        sid_b = url.split("session=")[1]
        r = self.client.post(f"/p/{ta}/test-checkout", data={"session": sid_b})
        self.assertEqual(r.status_code, 404)
        self.assertNotEqual(self.studio.order(b["id"])["payment_status"], "paid")

    def test_cannot_checkout_without_approval(self):
        token, req, vs = self.designed()
        r = self.client.post(f"/p/{token}/checkout", follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/p/{token}/approve")
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM checkouts")["n"], 0)


class FakeStripeHTTP:
    def __init__(self):
        self.sessions = {}

    def request(self, method, url, data=None, auth=None, headers=None, timeout=None):
        class R:
            def __init__(s, body, code=200):
                s.body, s.status_code = body, code

            def json(s):
                return s.body
        if method == "POST":
            sid = f"cs_test_{len(self.sessions) + 1}"
            self.sessions[sid] = {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}",
                                  "amount_total": int(data["line_items[0][price_data][unit_amount]"]),
                                  "currency": data["line_items[0][price_data][currency]"], "payment_status": "unpaid",
                                  "metadata": {"order_id": data["metadata[order_id]"]}}
            return R(self.sessions[sid])
        sid = url.rsplit("/", 1)[1]
        return R(self.sessions[sid])


class StripeTest(Base):
    def make_studio(self, **kw):
        self.http = FakeStripeHTTP()
        return Studio(self.env, payments=StripePayments("sk_test_x", "whsec_test", http=self.http))

    def signed(self, payload: bytes, secret="whsec_test", t=None):
        t = str(int(t or time.time()))
        sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}

    def checkout(self):
        token, req, vs = self.approved()
        r = self.client.post(f"/p/{token}/checkout", follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("https://checkout.stripe.com/"))
        sid = r.headers["location"].rsplit("/", 1)[1]
        return token, req, sid

    def event(self, sid, eid="evt_1", amount=None):
        s = dict(self.http.sessions[sid], payment_status="paid")
        if amount is not None:
            s["amount_total"] = amount
        return json.dumps({"id": eid, "type": "checkout.session.completed", "data": {"object": s}}).encode()

    def test_webhook_signature_and_idempotency(self):
        token, req, sid = self.checkout()
        body = self.event(sid)
        self.assertEqual(self.client.post("/webhooks/stripe", content=body,
                                          headers=self.signed(body, "wrong")).status_code, 400)
        self.assertEqual(self.client.post("/webhooks/stripe", content=body,
                                          headers=self.signed(body, t=time.time() - 3600)).status_code, 400)
        self.assertNotEqual(self.studio.order(req["id"])["payment_status"], "paid")
        r = self.client.post("/webhooks/stripe", content=body, headers=self.signed(body))
        self.assertEqual(r.json()["outcome"], "paid")
        r = self.client.post("/webhooks/stripe", content=body, headers=self.signed(body))    # Stripe retries
        self.assertEqual(r.json()["outcome"], "duplicate")
        self.http.sessions[sid]["payment_status"] = "paid"
        self.client.get(f"/p/{token}/paid?session_id={sid}")                                # redirect also lands
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM payment_events WHERE outcome='paid'")["n"], 1)
        self.assertEqual(self.studio.db.one("SELECT COUNT(*) n FROM emails WHERE kind='payment_confirmed'")["n"], 1)

    def test_return_url_confirms_server_side(self):
        token, req, sid = self.checkout()
        self.client.get(f"/p/{token}/paid?session_id={sid}")                      # Stripe says unpaid
        self.assertNotEqual(self.studio.order(req["id"])["payment_status"], "paid")
        self.http.sessions[sid]["payment_status"] = "paid"
        self.client.get(f"/p/{token}/paid?session_id={sid}")
        self.assertEqual(self.studio.order(req["id"])["payment_status"], "paid")

    def test_amount_mismatch_is_not_paid(self):
        token, req, sid = self.checkout()
        body = self.event(sid, amount=100)
        r = self.client.post("/webhooks/stripe", content=body, headers=self.signed(body))
        self.assertEqual(r.json()["outcome"], "mismatch")
        self.assertNotEqual(self.studio.order(req["id"])["payment_status"], "paid")

    def test_live_keys_refused_by_default(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_live_abc", "COLLAB_PAYMENT_PROVIDER": "stripe"}):
            with self.assertRaises(RuntimeError):
                load_env(self.tmp.name)


class FlakyProvider:
    name, is_demo = "flaky", False

    def __init__(self, fails, retryable=True):
        self.fails, self.retryable, self.calls = fails, retryable, 0

    def render(self, brief, direction, prompts):
        self.calls += 1
        if self.calls <= self.fails:
            raise design.ProviderError("service busy", retryable=self.retryable)
        return {"front": png((128, 22, 44)), "back": png((22, 32, 72))}


class GenerationTest(Base):
    def test_retry_then_success_and_colour_checks(self):
        self.studio.provider = FlakyProvider(fails=1)
        token, req = self.submit()
        self.studio.process_pending()
        j = self.studio.jobs(req["id"])[0]
        self.assertEqual((j["status"], j["attempts"]), ("queued", 1))
        self.assertIn("Retrying", j["stage"])
        self.studio.db.run("UPDATE jobs SET not_before=0")
        self.studio.process_pending()
        j = self.studio.jobs(req["id"])[0]
        self.assertEqual((j["status"], j["attempts"]), ("succeeded", 2))
        v = self.studio.versions(req["id"])[0]
        self.assertFalse(v["is_demo"])
        items = {i["check"]: i["status"] for i in v["checks"]["items"]}
        self.assertEqual(items["Colour: garnet"], "pass")
        self.assertEqual(items["Colour: navy"], "pass")
        self.assertEqual(items["Colour: silver"], "warn")
        self.assertEqual(items["Personalisation"], "review")

    def test_permanent_failure_is_reported_kindly(self):
        self.studio.provider = FlakyProvider(fails=99, retryable=False)
        token, req = self.submit()
        self.studio.process_pending()
        j = self.studio.jobs(req["id"])[0]
        self.assertEqual(j["status"], "failed")
        page = self.client.get(f"/p/{token}").text
        self.assertIn("We hit a snag", page)
        self.assertNotIn("service busy", page)                     # technical detail stays in admin
        self.admin()
        self.assertIn("service busy", self.client.get(f"/admin/r/{req['id']}").text)

    def test_generation_limit(self):
        token, req = self.submit()
        self.studio.process_pending()
        for _ in range(3):
            self.studio.regenerate(req["id"], "more contrast")
            self.studio.process_pending()
        with self.assertRaises(UserError):
            self.studio.regenerate(req["id"])
        self.assertIn("more contrast", self.studio.versions(req["id"])[-1]["prompt"])

    def test_manual_concept_upload(self):
        token, req = self.submit()
        vid = self.studio.upload_concept(req["id"], "C", png(), png())
        v = self.studio.version(req["id"], vid)
        self.assertEqual((v["provider"], v["is_demo"], v["state"]), ("manual", 0, "draft"))


class InterpretTest(unittest.TestCase):
    def test_brief_structure(self):
        st = {"vintage_waves": {"label": "Vintage waves"}}
        g = {"basketball": {"label": "Basketball jersey"}}
        b = design.interpret({"idea": "Kobe Bryant × Ronaldinho", "colors": "", "garment": "basketball",
                              "style": "vintage_waves", "back_name": "mamba", "back_number": "8"}, st, g)
        self.assertEqual(b["sides"], ["Kobe Bryant", "Ronaldinho"])
        self.assertEqual(b["recognised"], ["Kobe Bryant", "Ronaldinho"])
        self.assertEqual(b["palette_source"], "inspiration")
        self.assertIn("purple", b["palette"])
        self.assertEqual(b["back_name"], "MAMBA")
        self.assertEqual(len(b["directions"]), 2)
        self.assertNotEqual(b["directions"][0]["layout"], b["directions"][1]["layout"])
        p = design.concept_prompt(b, b["directions"][0], "back")
        self.assertIn('"MAMBA"', p)
        self.assertIn("No skeletons", p)


if __name__ == "__main__":
    unittest.main()
