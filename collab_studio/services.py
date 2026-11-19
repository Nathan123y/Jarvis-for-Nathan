"""Business rules. Routes call these; these call storage, design, payments, emails and fulfillment.

Invariants enforced here (and tested in tests/test_collab_studio.py):
- A customer can only reach their own request: lookup is by a secret token (HMAC of a per-request nonce).
- Design versions are never overwritten; revisions and replacements add new versions.
- An approval names one design version, one proof and the exact personalisation/size/quantity. Any change to those
  (new proof, new choice, new personalisation, revision) invalidates it, and production needs a valid approval that
  is also the one that was paid for.
- Payment events are recorded once per (provider, event id); orders are one per request.
- Production submission is claimed atomically and only when every gate passes.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import traceback

from . import design
from .config import DEFAULT_SETTINGS
from .db import DB, jloads, now
from .emails import Mailer
from .fulfillment import FulfillmentError, make_fulfillment
from .payments import make_payments
from .storage import MAX_REFERENCE_FILES, Storage, UploadError

STATUS_LABELS = {
    "received": "Request received",
    "designing": "Designing",
    "ready_for_review": "Ready for review",
    "revision_in_progress": "Revision in progress",
    "final_proof_ready": "Final proof ready",
    "awaiting_payment": "Awaiting payment",
    "preparing_production": "Preparing for production",
    "in_production": "In production",
    "shipped": "Shipped",
}
STATUS_ORDER = list(STATUS_LABELS)
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,13}$")
NUMBER_RE = re.compile(r"^\d{1,2}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


class UserError(ValueError):
    """Message is safe and friendly enough to show the customer or admin."""


def cents(amount) -> int:
    return int(round(float(amount) * 100))


class Studio:
    def __init__(self, env, db: DB = None, provider=None, payments=None, fulfillment=None):
        self.env = env
        self.db = db or DB(env.data_dir / "collab_studio.sqlite3")
        self.storage = Storage(self.db, env.data_dir / "files")
        self.mailer = Mailer(self.db, env)
        self.provider = provider or design.make_provider(env)
        self.payments = payments or make_payments(env)
        self.fulfillment = fulfillment or make_fulfillment(env)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._seed_settings()

    # ── settings ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _seed_settings(self):
        with self.db.tx() as c:
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))

    def settings(self) -> dict:
        s = {r["key"]: jloads(r["value"]) for r in self.db.all("SELECT key, value FROM settings")}
        return {**DEFAULT_SETTINGS, **s}

    def update_settings(self, changes: dict):
        with self.db.tx() as c:
            for k, v in changes.items():
                if k not in DEFAULT_SETTINGS:
                    raise UserError(f"Unknown setting {k}")
                c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))

    # ── tokens ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _token(self, nonce: str) -> str:
        return hmac.new(self.env.secret_key.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:43]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def token_for(self, req: dict) -> str:
        return self._token(req["token_nonce"])

    def link(self, req: dict, page: str = "") -> str:
        return f"{self.env.base_url}/p/{self.token_for(req)}{('/' + page) if page else ''}"

    def by_token(self, token: str):
        if not token or len(token) > 100:
            return None
        return self.db.one("SELECT * FROM requests WHERE token_hash=?", (self._hash(token),))

    def rotate_link(self, request_id: int):
        nonce = secrets.token_urlsafe(16)
        self.db.run("UPDATE requests SET token_nonce=?, token_hash=?, updated_at=? WHERE id=?",
                    (nonce, self._hash(self._token(nonce)), now(), request_id))
        req = self.get_request(request_id)
        self._email(req, "new_link", dedupe=f"link-{request_id}-{nonce}")
        return req

    # ── reads ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def get_request(self, rid: int):
        return self.db.one("SELECT * FROM requests WHERE id=?", (rid,))

    def versions(self, rid: int, customer: bool = False):
        rows = self.db.all("SELECT * FROM design_versions WHERE request_id=? ORDER BY version_no", (rid,))
        for r in rows:
            r["checks"] = jloads(r["checks"], {})
        if customer:
            rows = [r for r in rows if r["state"] in ("sent", "chosen", "replaced") and r["sent_at"]]
        return rows

    def version(self, rid: int, vid: int):
        v = self.db.one("SELECT * FROM design_versions WHERE id=? AND request_id=?", (vid, rid))
        if v:
            v["checks"] = jloads(v["checks"], {})
        return v

    def latest_brief(self, rid: int):
        b = self.db.one("SELECT * FROM briefs WHERE request_id=? ORDER BY version DESC LIMIT 1", (rid,))
        if b:
            b["data"] = jloads(b["data"], {})
        return b

    def jobs(self, rid: int):
        return self.db.all("SELECT * FROM jobs WHERE request_id=? ORDER BY id DESC", (rid,))

    def active_job(self, rid: int):
        return self.db.one("SELECT * FROM jobs WHERE request_id=? AND status IN ('queued','running') "
                           "ORDER BY id DESC LIMIT 1", (rid,))

    def current_proof(self, rid: int):
        return self.db.one("SELECT * FROM proofs WHERE request_id=? AND current=1 ORDER BY id DESC LIMIT 1", (rid,))

    def current_approval(self, rid: int):
        return self.db.one("SELECT * FROM approvals WHERE request_id=? AND valid=1 ORDER BY id DESC LIMIT 1", (rid,))

    def order(self, rid: int):
        o = self.db.one("SELECT * FROM orders WHERE request_id=?", (rid,))
        if o:
            o["shipping"] = jloads(o["shipping"], {})
        return o

    def updates(self, rid: int, customer: bool = True):
        q = "SELECT * FROM updates WHERE request_id=?" + (" AND customer_visible=1" if customer else "")
        return self.db.all(q + " ORDER BY id DESC", (rid,))

    def revisions(self, rid: int):
        return self.db.all("SELECT * FROM revisions WHERE request_id=? ORDER BY id", (rid,))

    # ── 1. submit a request ───────────────────────────────────────────────────────────────────────────────────────
    def create_request(self, form: dict, files: list) -> tuple:
        """files: [(filename, bytes)]. Returns (request row, private token)."""
        st = self.settings()
        f = {k: (form.get(k) or "").strip() for k in
             ("customer_name", "email", "garment", "idea", "colors", "style", "back_name", "back_number",
              "instructions")}
        errors = {}
        if not 2 <= len(f["idea"]) <= 500:
            errors["idea"] = "Tell us your idea in a few words (up to 500 characters)."
        g = st["garments"].get(f["garment"])
        if not g or not g.get("enabled", True):
            errors["garment"] = "Choose a garment."
        if f["style"] not in st["styles"]:
            errors["style"] = "Choose a style."
        if f["back_name"] and not NAME_RE.match(f["back_name"]):
            errors["back_name"] = "Back name: letters only, up to 14 characters."
        if f["back_number"] and not NUMBER_RE.match(f["back_number"]):
            errors["back_number"] = "Back number: 0–99."
        if not 1 <= len(f["customer_name"]) <= 80:
            errors["customer_name"] = "Please add your name."
        if not EMAIL_RE.match(f["email"]):
            errors["email"] = "Please add a valid email address."
        if len(f["colors"]) > 200 or len(f["instructions"]) > 2000:
            errors["instructions"] = "That's a bit long — please shorten it."
        files = [(n, b) for n, b in files if b]
        if len(files) > MAX_REFERENCE_FILES:
            errors["references"] = f"Up to {MAX_REFERENCE_FILES} reference images."
        else:
            from .storage import sniff
            for n, b in files:
                try:
                    sniff(b)
                except UploadError as e:
                    errors["references"] = f"{n}: {e}"
        if errors:
            raise FormErrors(errors)
        own = 1 if form.get("own_brand") in ("1", "on", "true", True) else 0
        nonce = secrets.token_urlsafe(16)
        token = self._token(nonce)
        t = now()
        with self.db.tx() as c:
            for _ in range(5):
                ref = "CS-" + "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
                if not c.execute("SELECT 1 FROM requests WHERE ref=?", (ref,)).fetchone():
                    break
            rid = c.execute(
                "INSERT INTO requests (ref, token_hash, token_nonce, customer_name, email, garment, idea, colors, style,"
                " back_name, back_number, instructions, own_brand, status, revision_limit, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, self._hash(token), nonce, f["customer_name"], f["email"].lower(), f["garment"], f["idea"],
                 f["colors"], f["style"], f["back_name"].upper(), f["back_number"], f["instructions"], own,
                 "received", int(st["revision_limit"]), t, t)).lastrowid
            for n, b in files:
                self.storage.save(rid, "reference", b, original_name=n)
            req = dict(c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
            brief = self._interpret(req)
            c.execute("INSERT INTO briefs (request_id, version, data, source, created_at) VALUES (?,?,?,?,?)",
                      (rid, 1, json.dumps(brief), "interpreter", t))
            if brief["third_party_branding"]:
                c.execute("UPDATE requests SET branding_status='pending' WHERE id=?", (rid,))
            self._add_update(rid, "We've received your idea and added it to the design queue.")
            self._enqueue(rid, "initial", count=True)
        req = self.get_request(rid)
        self._email(req, "request_received", dedupe=f"received-{rid}")
        return req, token

    def _interpret(self, req):
        st = self.settings()
        req = dict(req)
        req["_reference_count"] = len(self.storage.list(req["id"], "reference"))
        return design.interpret(req, st["styles"], st["garments"])

    def _add_update(self, rid, message, visible=True):
        self.db.run("INSERT INTO updates (request_id, message, customer_visible, created_at) VALUES (?,?,?,?)",
                    (rid, message, 1 if visible else 0, now()))

    def _set_status(self, rid, status):
        self.db.run("UPDATE requests SET status=?, updated_at=? WHERE id=?", (status, now(), rid))

    def _email(self, req, kind, dedupe=None, page="", **extra):
        st = self.settings()
        ctx = {"ref": req["ref"], "name": req["customer_name"].split()[0], "link": self.link(req, page), **extra}
        return self.mailer.send(kind, req["email"], ctx, st["business_name"], request_id=req["id"], dedupe_key=dedupe)

    # ── 2–3. design jobs ──────────────────────────────────────────────────────────────────────────────────────────
    def _enqueue(self, rid, kind, count=False, base_version_id=None, revision_id=None):
        st = self.settings()
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM jobs WHERE request_id=? AND status IN ('queued','running')", (rid,)).fetchone():
                raise UserError("A design job is already running for this request.")
            if count:
                used = c.execute("SELECT generations_used FROM requests WHERE id=?", (rid,)).fetchone()[0]
                if used >= int(st["max_generations_per_request"]):
                    raise UserError("This request has reached its generation limit. Raise it in Settings or upload "
                                    "a concept manually.")
                c.execute("UPDATE requests SET generations_used=generations_used+1 WHERE id=?", (rid,))
            t = now()
            jid = c.execute("INSERT INTO jobs (request_id, kind, status, max_attempts, stage, base_version_id, "
                            "revision_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (rid, kind, "queued", int(st["generation_max_attempts"]), "Waiting in the design queue",
                             base_version_id, revision_id, t, t)).lastrowid
        self._wake.set()
        return jid

    def regenerate(self, rid, admin_instructions=None):
        if admin_instructions is not None:
            self.db.run("UPDATE requests SET admin_instructions=?, updated_at=? WHERE id=?",
                        (admin_instructions.strip()[:4000], now(), rid))
        req = self.get_request(rid)
        brief = self._interpret(req)
        prev = self.latest_brief(rid)
        self.db.run("INSERT INTO briefs (request_id, version, data, source, created_at) VALUES (?,?,?,?,?)",
                    (rid, (prev["version"] if prev else 0) + 1, json.dumps(brief), "admin", now()))
        jid = self._enqueue(rid, "regenerate", count=True)
        if req["status"] in ("received",):
            self._set_status(rid, "designing")
        return jid

    def claim_next_job(self):
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE status='queued' AND not_before<=? ORDER BY id LIMIT 1",
                            (now(),)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, progress=5, stage=?, error='', "
                      "updated_at=? WHERE id=? AND status='queued'", ("Reading your idea", now(), row["id"]))
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def _progress(self, jid, pct, stage):
        self.db.run("UPDATE jobs SET progress=?, stage=?, updated_at=? WHERE id=?", (pct, stage, now(), jid))

    def run_job(self, job):
        rid = job["request_id"]
        req = self.get_request(rid)
        try:
            if req["status"] == "received":
                self._set_status(rid, "designing")
            if job["kind"] == "revision":
                base = self.version(rid, job["base_version_id"])
                rev = self.db.one("SELECT * FROM revisions WHERE id=?", (job["revision_id"],))
                base_brief = self.db.one("SELECT * FROM briefs WHERE id=?", (base["brief_id"],)) if base["brief_id"] \
                    else None
                bdata = jloads(base_brief["data"], {}) if base_brief else self.latest_brief(rid)["data"]
                key = base["label"][0]
                brief = design.revise_brief(bdata, key, rev["note"])
                bid = self._store_brief(rid, brief, "revision")
                plan = [(brief["directions"][0], f"{key}{job_round(self, rid) + 1}", base["id"])]
            else:
                b = self.latest_brief(rid)
                brief, bid = b["data"], b["id"]
                plan = [(d, d["key"], None) for d in brief["directions"]]
            self._progress(job["id"], 15, "Writing the design brief")
            rnd = job_round(self, rid) + 1
            made = []
            for i, (direction, label, parent) in enumerate(plan):
                self._progress(job["id"], 20 + int(60 * i / len(plan)), f"Creating concept {label}")
                prompts = {v: design.concept_prompt(brief, direction, v) for v in ("front", "back")}
                images = self.provider.render(brief, direction, prompts)
                checks = design.run_checks(brief, images, self.provider.name, self.provider.is_demo)
                made.append((direction, label, parent, prompts, images, checks))
            self._progress(job["id"], 85, "Checking colours, lettering and garment")
            st = self.settings()
            with self.db.tx() as c:
                for direction, label, parent, prompts, images, checks in made:
                    fa = self.storage.save(rid, "concept", images["front"], panel="front")
                    ba = self.storage.save(rid, "concept", images["back"], panel="back")
                    self._insert_version(c, rid, label, rnd, parent, bid, job["id"],
                                         "FRONT:\n" + prompts["front"] + "\n\nBACK:\n" + prompts["back"],
                                         self.provider.name, self.provider.is_demo, fa, ba, checks)
                c.execute("UPDATE jobs SET status='succeeded', progress=100, stage='Done', updated_at=? WHERE id=?",
                          (now(), job["id"]))
            if not st["require_admin_review"]:
                drafts = [v["id"] for v in self.versions(rid) if v["state"] == "draft" and v["job_id"] == job["id"]]
                self.send_versions(rid, drafts)
            else:
                self._add_update(rid, "New concepts are waiting for designer review.", visible=False)
        except Exception as e:   # noqa: BLE001 — every failure is recorded on the job
            retryable = getattr(e, "retryable", True) and not isinstance(e, (UserError, KeyError, TypeError))
            attempts, max_attempts = job["attempts"], job["max_attempts"]
            msg = str(e) or type(e).__name__
            if retryable and attempts < max_attempts:
                delay = min(600, 20 * 2 ** (attempts - 1))
                self.db.run("UPDATE jobs SET status='queued', not_before=?, stage=?, error=?, updated_at=? WHERE id=?",
                            (now() + delay, f"Retrying shortly (attempt {attempts + 1} of {max_attempts})", msg[:500],
                             now(), job["id"]))
            else:
                self.db.run("UPDATE jobs SET status='failed', stage='Stopped', error=?, updated_at=? WHERE id=?",
                            ((msg + "\n" + traceback.format_exc(limit=3))[:2000], now(),
                             job["id"]))
                self._add_update(rid, f"Design job failed: {msg[:200]}", visible=False)

    def _store_brief(self, rid, brief, source):
        prev = self.latest_brief(rid)
        return self.db.run("INSERT INTO briefs (request_id, version, data, source, created_at) VALUES (?,?,?,?,?)",
                           (rid, (prev["version"] if prev else 0) + 1, json.dumps(brief), source, now()))

    def _insert_version(self, c, rid, label, rnd, parent, brief_id, job_id, prompt, provider, is_demo, fa, ba, checks):
        vno = (c.execute("SELECT COALESCE(MAX(version_no),0) FROM design_versions WHERE request_id=?",
                         (rid,)).fetchone()[0]) + 1
        return c.execute(
            "INSERT INTO design_versions (request_id, version_no, label, round, parent_id, brief_id, job_id, prompt, "
            "provider, is_demo, front_asset_id, back_asset_id, checks, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, vno, label, rnd, parent, brief_id, job_id, prompt, provider, 1 if is_demo else 0, fa, ba,
             json.dumps(checks), "draft", now())).lastrowid

    def process_pending(self, limit=10):
        """Run queued jobs now (used by tests and the background thread)."""
        n = 0
        while n < limit:
            job = self.claim_next_job()
            if not job:
                break
            self.run_job(job)
            n += 1
        return n

    def start_worker(self):
        def loop():
            while not self._stop.is_set():
                try:
                    if not self.process_pending(limit=1):
                        self._wake.wait(2)
                        self._wake.clear()
                except Exception:   # noqa: BLE001 — keep the worker alive; the job row holds the error
                    time.sleep(2)
        # a job left 'running' by a crash goes back in the queue
        self.db.run("UPDATE jobs SET status='queued', stage='Restarted after an interruption' WHERE status='running'")
        t = threading.Thread(target=loop, name="collab-design-worker", daemon=True)
        t.start()
        return t

    def stop_worker(self):
        self._stop.set()
        self._wake.set()

    # ── admin: concepts ───────────────────────────────────────────────────────────────────────────────────────────
    def upload_concept(self, rid, label, front: bytes, back: bytes, note=""):
        label = (label or "").strip()[:6] or "M"
        fa = self.storage.save(rid, "concept", front, panel="front")
        ba = self.storage.save(rid, "concept", back, panel="back")
        b = self.latest_brief(rid)
        checks = design.run_checks(b["data"], {"front": self.read(fa), "back": self.read(ba)}, "manual", False)
        with self.db.tx() as c:
            vid = self._insert_version(c, rid, label, job_round(self, rid) + 1, None, b["id"], None,
                                       f"Uploaded by the designer. {note}".strip(), "manual", False, fa, ba, checks)
        return vid

    def send_versions(self, rid, version_ids):
        if not version_ids:
            raise UserError("Pick at least one concept to send.")
        req = self.get_request(rid)
        vs = [self.version(rid, int(v)) for v in version_ids]
        if any(v is None or v["state"] not in ("draft", "held") for v in vs):
            raise UserError("Only draft concepts can be sent.")
        t = now()
        with self.db.tx() as c:
            for v in vs:
                c.execute("UPDATE design_versions SET state='sent', sent_at=? WHERE id=?", (t, v["id"]))
            c.execute("UPDATE requests SET status='ready_for_review', updated_at=? WHERE id=?", (t, rid))
        is_rev = any(v["parent_id"] for v in vs) or req["revisions_used"] > 0
        self._add_update(rid, "Your revised design is ready to review." if is_rev else "Your concepts are ready to review.")
        self._email(req, "revision_ready" if is_rev else "designs_ready",
                    dedupe="sent-" + "-".join(str(v["id"]) for v in vs))

    def hold_version(self, rid, vid):
        self.db.run("UPDATE design_versions SET state='held' WHERE id=? AND request_id=? AND state='draft'", (vid, rid))

    # ── customer: choose / revise ─────────────────────────────────────────────────────────────────────────────────
    def _locked_after_payment(self, rid):
        o = self.order(rid)
        if o and o["payment_status"] == "paid":
            raise UserError("Your order is paid, so the design is locked. Reply to your confirmation email if you "
                            "need to change something.")

    def choose(self, rid, vid):
        self._locked_after_payment(rid)
        v = self.version(rid, vid)
        if not v or v["state"] not in ("sent", "chosen", "replaced") or not v["sent_at"]:
            raise UserError("That design isn't available.")
        if self.active_job(rid):
            raise UserError("A revision is still in progress. You can choose once it's ready.")
        st = self.settings()
        req = self.get_request(rid)
        with self.db.tx() as c:
            c.execute("UPDATE design_versions SET state='sent' WHERE request_id=? AND state='chosen'", (rid,))
            c.execute("UPDATE design_versions SET state='chosen' WHERE id=?", (vid,))
            c.execute("UPDATE requests SET chosen_version_id=?, updated_at=? WHERE id=?", (vid, now(), rid))
            self._invalidate_approvals(c, rid, "A different design was chosen.")
            c.execute("UPDATE proofs SET current=0 WHERE request_id=?", (rid,))
            if st["require_admin_proof"]:
                c.execute("UPDATE requests SET status='designing' WHERE id=?", (rid,))
            else:
                c.execute("INSERT INTO proofs (request_id, version_id, front_asset_id, back_asset_id, source, back_name,"
                          " back_number, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (rid, vid, v["front_asset_id"], v["back_asset_id"], "concept", req["back_name"],
                           req["back_number"], now()))
                c.execute("UPDATE requests SET status='final_proof_ready' WHERE id=?", (rid,))
        self._add_update(rid, f"You chose design {v['label']} (version {v['version_no']}).")
        if st["require_admin_proof"]:
            self._add_update(rid, "We're preparing your final proof.")

    def request_revision(self, rid, vid, note):
        self._locked_after_payment(rid)
        note = (note or "").strip()
        if not 5 <= len(note) <= 2000:
            raise UserError("Describe the changes you'd like (at least a few words).")
        req = self.get_request(rid)
        v = self.version(rid, vid)
        if not v or not v["sent_at"]:
            raise UserError("That design isn't available.")
        if req["revisions_used"] >= req["revision_limit"]:
            raise UserError("You've used your included revisions. Reply to your design email and we'll help.")
        if self.active_job(rid):
            raise UserError("A design job is already in progress.")
        with self.db.tx() as c:
            rev = c.execute("INSERT INTO revisions (request_id, version_id, note, created_at) VALUES (?,?,?,?)",
                            (rid, vid, note, now())).lastrowid
            c.execute("UPDATE requests SET revisions_used=revisions_used+1, status='revision_in_progress', "
                      "updated_at=? WHERE id=?", (now(), rid))
            self._invalidate_approvals(c, rid, "Customer requested a revision.")
            self._enqueue(rid, "revision", base_version_id=vid, revision_id=rev)
        self._add_update(rid, f"We're working on your changes to design {v['label']}.")

    def grant_revision(self, rid, extra=1):
        self.db.run("UPDATE requests SET revision_limit=revision_limit+?, updated_at=? WHERE id=?", (extra, now(), rid))

    # ── proofs / approval ────────────────────────────────────────────────────────────────────────────────────────
    def _invalidate_approvals(self, c, rid, reason):
        c.execute("UPDATE approvals SET valid=0, invalidated_reason=? WHERE request_id=? AND valid=1", (reason, rid))

    def upload_proof(self, rid, front: bytes, back: bytes):
        req = self.get_request(rid)
        if not req["chosen_version_id"]:
            raise UserError("The customer hasn't chosen a design yet.")
        fa = self.storage.save(rid, "proof", front, panel="front")
        ba = self.storage.save(rid, "proof", back, panel="back")
        with self.db.tx() as c:
            c.execute("UPDATE proofs SET current=0 WHERE request_id=?", (rid,))
            pid = c.execute("INSERT INTO proofs (request_id, version_id, front_asset_id, back_asset_id, source, "
                            "back_name, back_number, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (rid, req["chosen_version_id"], fa, ba, "admin_upload", req["back_name"],
                             req["back_number"], now())).lastrowid
            self._invalidate_approvals(c, rid, "A new final proof was uploaded.")
            if req["status"] not in ("in_production", "shipped"):
                c.execute("UPDATE requests SET status='final_proof_ready', updated_at=? WHERE id=?", (now(), rid))
        self._add_update(rid, "Your final proof is ready to approve.")
        self._email(req, "proof_ready", dedupe=f"proof-{pid}", page="approve")
        return pid

    def set_personalisation(self, rid, back_name, back_number, by_admin=True):
        back_name, back_number = (back_name or "").strip().upper(), (back_number or "").strip()
        if back_name and not NAME_RE.match(back_name):
            raise UserError("Back name: letters only, up to 14 characters.")
        if back_number and not NUMBER_RE.match(back_number):
            raise UserError("Back number: 0–99.")
        with self.db.tx() as c:
            c.execute("UPDATE requests SET back_name=?, back_number=?, updated_at=? WHERE id=?",
                      (back_name, back_number, now(), rid))
            self._invalidate_approvals(c, rid, "Personalisation changed.")
            # a proof that shows the old name/number can't be approved any more
            c.execute("UPDATE proofs SET current=0 WHERE request_id=? AND current=1 AND (back_name!=? OR back_number!=?)",
                      (rid, back_name, back_number))
        if not self.current_proof(rid):
            req = self.get_request(rid)
            if req["chosen_version_id"] and req["status"] not in ("in_production", "shipped"):
                self._set_status(rid, "designing")
                self._add_update(rid, "We're updating your proof with your new name and number.")
                self._add_update(rid, "Personalisation changed — upload an updated proof.", visible=False)

    @staticmethod
    def fingerprint(version_id, proof, garment, back_name, back_number, size, quantity):
        raw = json.dumps([version_id, proof["id"], proof["front_asset_id"], proof["back_asset_id"], garment,
                          back_name, back_number, size, quantity])
        return hashlib.sha256(raw.encode()).hexdigest()

    def approve(self, rid, version_id, proof_id, back_name, back_number, size, quantity, confirmed):
        self._locked_after_payment(rid)
        if not confirmed:
            raise UserError("Please tick the box to confirm you approve this exact design.")
        req = self.get_request(rid)
        st = self.settings()
        g = st["garments"][req["garment"]]
        proof = self.current_proof(rid)
        if not proof or proof["id"] != int(proof_id) or proof["version_id"] != int(version_id) \
                or req["chosen_version_id"] != int(version_id):
            raise UserError("This proof has changed since you opened the page. Please review the latest version.")
        if size not in g["sizes"]:
            raise UserError("Please choose a size.")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            quantity = 0
        if not 1 <= quantity <= 20:
            raise UserError("Quantity must be between 1 and 20.")
        back_name, back_number = (back_name or "").strip().upper(), (back_number or "").strip()
        if (back_name, back_number) != (proof["back_name"], proof["back_number"]):
            self.set_personalisation(rid, back_name, back_number, by_admin=False)
            raise ProofChanged()
        fp = self.fingerprint(proof["version_id"], proof, req["garment"], back_name, back_number, size, quantity)
        q = self.quote(req["garment"], quantity)
        with self.db.tx() as c:
            self._invalidate_approvals(c, rid, "Replaced by a newer approval.")
            aid = c.execute("INSERT INTO approvals (request_id, version_id, proof_id, garment, back_name, back_number, "
                            "size, quantity, fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (rid, proof["version_id"], proof["id"], req["garment"], back_name, back_number, size,
                             quantity, fp, now())).lastrowid
            t = now()
            if c.execute("SELECT 1 FROM orders WHERE request_id=?", (rid,)).fetchone():
                c.execute("UPDATE orders SET approval_id=?, currency=?, unit_price_cents=?, quantity=?, "
                          "subtotal_cents=?, shipping_cents=?, tax_cents=?, total_cents=?, prices_are_demo=?, "
                          "updated_at=? WHERE request_id=? AND payment_status!='paid'",
                          (aid, q["currency"], q["unit_cents"], quantity, q["subtotal_cents"], q["shipping_cents"],
                           q["tax_cents"], q["total_cents"], 1 if q["demo"] else 0, t, rid))
            else:
                c.execute("INSERT INTO orders (request_id, approval_id, currency, unit_price_cents, quantity, "
                          "subtotal_cents, shipping_cents, tax_cents, total_cents, prices_are_demo, created_at, "
                          "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, aid, q["currency"], q["unit_cents"], quantity, q["subtotal_cents"],
                           q["shipping_cents"], q["tax_cents"], q["total_cents"], 1 if q["demo"] else 0, t, t))
            c.execute("UPDATE requests SET status='awaiting_payment', updated_at=? WHERE id=?", (t, rid))
        v = self.version(rid, proof["version_id"])
        self._add_update(rid, f"You approved design {v['label']} (version {v['version_no']}), size {size}, "
                              f"quantity {quantity}.")
        return aid

    def quote(self, garment, quantity):
        st = self.settings()
        g = st["garments"][garment]
        unit = cents(g["price"])
        sub = unit * quantity
        ship = cents(g["shipping"])
        rate = st.get("tax_rate_percent")
        tax = int(round((sub + ship) * float(rate) / 100)) if rate not in (None, "") else None
        return {"currency": st["currency"], "unit_cents": unit, "subtotal_cents": sub, "shipping_cents": ship,
                "tax_cents": tax, "total_cents": sub + ship + (tax or 0), "demo": bool(st["prices_are_demo"])}

    # ── payment ───────────────────────────────────────────────────────────────────────────────────────────────────
    def start_checkout(self, rid):
        req = self.get_request(rid)
        o = self.order(rid)
        a = self.current_approval(rid)
        if not o or not a or o["approval_id"] != a["id"]:
            raise UserError("Please approve your final proof before paying.")
        if o["payment_status"] == "paid":
            raise UserError("This order is already paid.")
        st = self.settings()
        g = st["garments"][req["garment"]]
        title = f"{st['business_name']} {g['label']} — {req['ref']} (size {a['size']} × {a['quantity']})"
        token = self.token_for(req)
        base = f"{self.env.base_url}/p/{token}"
        s = self.payments.create_checkout(o, a, title, success_url=f"{base}/paid", cancel_url=f"{base}/approve",
                                          test_page_url=f"{base}/test-checkout?session={{session}}",
                                          countries=tuple(st.get("ship_countries") or ["US"]))
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO checkouts (id, provider, order_id, approval_id, amount_cents, currency, "
                      "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (s["id"], self.payments.name, o["id"], a["id"], o["total_cents"], o["currency"], "open", now()))
            c.execute("UPDATE orders SET checkout_ref=?, payment_status='pending', updated_at=? WHERE id=? "
                      "AND payment_status!='paid'", (s["id"], now(), o["id"]))
        return s["url"]

    def confirm_checkout(self, session_id, event_id=None, provider_view=None, shipping=None):
        """Server-side confirmation. provider_view: {'paid', 'amount_cents', 'currency'} from the provider (Stripe
        retrieve / webhook). For the simulated provider it is None and the stored checkout is the source of truth.
        Returns one of: paid | duplicate | already_paid | not_paid | mismatch | unknown."""
        chk = self.db.one("SELECT * FROM checkouts WHERE id=?", (session_id,))
        if not chk:
            return "unknown"
        if provider_view is not None:
            if not provider_view.get("paid"):
                return "not_paid"
            if provider_view.get("amount_cents") != chk["amount_cents"] or \
                    (provider_view.get("currency") or "").lower() != chk["currency"].lower():
                self._record_event(chk["provider"], event_id or f"mismatch-{session_id}", chk["order_id"], "mismatch",
                                   provider_view.get("amount_cents"), "mismatch")
                return "mismatch"
        ev = event_id or f"checkout:{session_id}"
        with self.db.tx() as c:
            try:
                c.execute("INSERT INTO payment_events (provider, event_id, order_id, kind, amount_cents, outcome, "
                          "created_at) VALUES (?,?,?,?,?,?,?)",
                          (chk["provider"], ev, chk["order_id"], "checkout.completed", chk["amount_cents"], "received",
                           now()))
            except sqlite3.IntegrityError:
                return "duplicate"
            # the same session can be confirmed by both the redirect and the webhook: one wins
            claimed = c.execute("UPDATE checkouts SET status='paid' WHERE id=? AND status!='paid'", (session_id,))
            if claimed.rowcount == 0:
                c.execute("UPDATE payment_events SET outcome='duplicate' WHERE provider=? AND event_id=?",
                          (chk["provider"], ev))
                return "duplicate"
            o = dict(c.execute("SELECT * FROM orders WHERE id=?", (chk["order_id"],)).fetchone())
            if o["payment_status"] == "paid":
                c.execute("UPDATE payment_events SET outcome='already_paid' WHERE provider=? AND event_id=?",
                          (chk["provider"], ev))
                return "already_paid"
            c.execute("UPDATE orders SET payment_status='paid', paid_at=?, paid_approval_id=?, paid_amount_cents=?, "
                      "shipping=?, updated_at=? WHERE id=?",
                      (now(), chk["approval_id"], chk["amount_cents"], json.dumps(shipping or {}), now(), o["id"]))
            c.execute("UPDATE payment_events SET outcome='paid' WHERE provider=? AND event_id=?", (chk["provider"], ev))
            c.execute("UPDATE requests SET status='preparing_production', updated_at=? WHERE id=?",
                      (now(), o["request_id"]))
        req = self.get_request(o["request_id"])
        self._add_update(req["id"], "Payment confirmed. We're preparing your order for production.")
        self._email(req, "payment_confirmed", dedupe=f"paid-{o['id']}", page="order")
        return "paid"

    def _record_event(self, provider, event_id, order_id, kind, amount, outcome):
        try:
            self.db.run("INSERT INTO payment_events (provider, event_id, order_id, kind, amount_cents, outcome, "
                        "created_at) VALUES (?,?,?,?,?,?,?)", (provider, event_id, order_id, kind, amount, outcome,
                                                               now()))
        except sqlite3.IntegrityError:
            pass

    # ── production ────────────────────────────────────────────────────────────────────────────────────────────────
    def latest_artwork(self, rid):
        out = {}
        for a in self.storage.list(rid, "production"):
            out[a["panel"]] = a                 # later uploads win
        return out

    def validate_artwork(self, rid):
        """[(panel, status, detail)] against the garment's configured panels. Requirements come only from Settings."""
        req = self.get_request(rid)
        g = self.settings()["garments"][req["garment"]]
        art = self.latest_artwork(rid)
        report = []
        for p in g.get("panels", []):
            a = art.get(p["name"])
            if not a:
                report.append((p["name"], "fail", "missing"))
                continue
            problems = []
            if p.get("min_width") and (a["width"] or 0) < int(p["min_width"]):
                problems.append(f"width {a['width']} < {p['min_width']}")
            if p.get("min_height") and (a["height"] or 0) < int(p["min_height"]):
                problems.append(f"height {a['height']} < {p['min_height']}")
            if p.get("width") and p.get("height") and a["width"] and a["height"]:
                want, got = int(p["width"]) / int(p["height"]), a["width"] / a["height"]
                if abs(want - got) / want > 0.02:
                    problems.append(f"aspect {got:.3f} ≠ template {want:.3f}")
            if problems:
                report.append((p["name"], "fail", "; ".join(problems)))
            elif any(p.get(k) for k in ("min_width", "min_height", "width", "height")):
                report.append((p["name"], "pass", f"{a['width']}×{a['height']}px"))
            else:
                report.append((p["name"], "review", f"{a['width']}×{a['height']}px — no manufacturer size set, "
                                                    "check against the template"))
        return report

    def upload_artwork(self, rid, panel, data, name=""):
        req = self.get_request(rid)
        panels = [p["name"] for p in self.settings()["garments"][req["garment"]].get("panels", [])]
        if panel not in panels:
            raise UserError(f"Unknown panel {panel}.")
        aid = self.storage.save(rid, "production", data, original_name=name, panel=panel)
        self.db.run("UPDATE orders SET artwork_status='needs_review', updated_at=? WHERE request_id=?", (now(), rid))
        return aid

    def set_artwork_review(self, rid, passed: bool, notes=""):
        if passed and any(s == "fail" for _, s, _ in self.validate_artwork(rid)):
            raise UserError("Artwork has failing checks. Fix or replace the panels first.")
        o = self.order(rid)
        if not o:
            raise UserError("There's no order yet.")
        self.db.run("UPDATE orders SET artwork_status=?, artwork_notes=?, updated_at=? WHERE id=?",
                    ("passed" if passed else "failed", notes[:2000], now(), o["id"]))

    def set_branding(self, rid, status, notes=""):
        if status not in ("not_needed", "pending", "cleared", "declined"):
            raise UserError("Unknown branding status.")
        self.db.run("UPDATE requests SET branding_status=?, branding_notes=?, updated_at=? WHERE id=?",
                    (status, notes[:2000], now(), rid))

    def production_blockers(self, rid):
        req = self.get_request(rid)
        o = self.order(rid)
        a = self.current_approval(rid)
        proof = self.current_proof(rid)
        out = []
        if not o or o["payment_status"] != "paid":
            out.append("Payment isn't confirmed.")
        if not a:
            out.append("There's no valid customer approval of the final proof.")
        else:
            if not proof or a["proof_id"] != proof["id"]:
                out.append("The approval is for an older proof.")
            if a["version_id"] != req["chosen_version_id"]:
                out.append("The approval is for a different design version.")
            if o and o["payment_status"] == "paid" and o["paid_approval_id"] != a["id"]:
                out.append("The customer paid for a different approval than the current one — confirm with them.")
        if not o or o["artwork_status"] != "passed":
            out.append("Production artwork hasn't passed review.")
        if req["branding_status"] in ("pending", "declined"):
            out.append("Branding permission is unresolved." if req["branding_status"] == "pending"
                       else "Branding permission was declined.")
        if o and o["production_status"] != "not_submitted":
            out.append("Already submitted to production.")
        return out

    def submit_to_production(self, rid, external_ref=""):
        blockers = self.production_blockers(rid)
        if blockers:
            raise UserError("Can't submit yet: " + " ".join(blockers))
        o = self.order(rid)
        with self.db.tx() as c:
            claimed = c.execute("UPDATE orders SET production_status='submitting', updated_at=? WHERE id=? AND "
                                "production_status='not_submitted' AND payment_status='paid'", (now(), o["id"]))
            if claimed.rowcount == 0:
                raise UserError("Already submitted to production.")
        req = self.get_request(rid)
        a = self.current_approval(rid)
        g = self.settings()["garments"][req["garment"]]
        try:
            res = self.fulfillment.submit(o, req, a, g, self.latest_artwork(rid), self.read_asset, external_ref)
        except FulfillmentError as e:
            self.db.run("UPDATE orders SET production_status='not_submitted', updated_at=? WHERE id=?", (now(), o["id"]))
            raise UserError(str(e))
        with self.db.tx() as c:
            c.execute("UPDATE orders SET production_status='submitted', fulfillment_provider=?, fulfillment_ref=?, "
                      "submitted_at=?, updated_at=? WHERE id=?",
                      (self.fulfillment.name, res["ref"], now(), now(), o["id"]))
            c.execute("UPDATE requests SET status='in_production', updated_at=? WHERE id=?", (now(), rid))
        self._add_update(rid, "Your order is in production.")
        return res

    def record_cost(self, rid, amount):
        self.db.run("UPDATE orders SET manufacturing_cost_cents=?, updated_at=? WHERE request_id=?",
                    (cents(amount) if amount not in ("", None) else None, now(), rid))

    def add_tracking(self, rid, carrier, number, url=""):
        o = self.order(rid)
        if not o or o["production_status"] not in ("submitted", "shipped"):
            raise UserError("Submit the order to production before adding tracking.")
        number = (number or "").strip()
        if not number:
            raise UserError("Add a tracking number.")
        if url and not url.startswith("https://"):
            raise UserError("Tracking links must start with https://")
        with self.db.tx() as c:
            c.execute("UPDATE orders SET carrier=?, tracking_number=?, tracking_url=?, production_status='shipped', "
                      "updated_at=? WHERE id=?", (carrier.strip()[:60], number[:80], url[:500], now(), o["id"]))
            c.execute("UPDATE requests SET status='shipped', updated_at=? WHERE id=?", (now(), rid))
        req = self.get_request(rid)
        self._add_update(rid, f"Shipped with {carrier or 'the carrier'} — tracking {number}.")
        self._email(req, "order_shipped", dedupe=f"shipped-{o['id']}-{number}", page="order",
                    tracking=f"Carrier: {carrier or '—'} · Tracking number: {number}")

    def post_update(self, rid, message):
        message = (message or "").strip()
        if not message:
            raise UserError("Write an update first.")
        self._add_update(rid, message[:1000])

    # ── assets ────────────────────────────────────────────────────────────────────────────────────────────────────
    def read(self, asset_id):
        return self.storage.path_of(self.storage.get(asset_id)).read_bytes()

    def read_asset(self, asset):
        return self.storage.path_of(asset).read_bytes()

    def customer_can_see(self, rid, asset_id) -> bool:
        """Customers see their references, sent concepts and current/past proofs — never drafts or production files."""
        a = self.storage.get(asset_id)
        if not a or a["request_id"] != rid:
            return False
        if a["kind"] == "reference":
            return True
        if a["kind"] == "concept":
            return bool(self.db.one("SELECT 1 FROM design_versions WHERE request_id=? AND sent_at IS NOT NULL AND "
                                    "(front_asset_id=? OR back_asset_id=?)", (rid, asset_id, asset_id)))
        if a["kind"] == "proof":
            return bool(self.db.one("SELECT 1 FROM proofs WHERE request_id=? AND (front_asset_id=? OR back_asset_id=?)",
                                    (rid, asset_id, asset_id)))
        return False


class FormErrors(UserError):
    def __init__(self, errors):
        super().__init__("Please fix the highlighted fields.")
        self.errors = errors


class ProofChanged(UserError):
    def __init__(self):
        super().__init__("Thanks — your name/number changed, so we'll update your proof and ask you to approve it "
                         "again. Nothing has been charged.")


def job_round(studio, rid):
    r = studio.db.one("SELECT COALESCE(MAX(round),0) AS r FROM design_versions WHERE request_id=?", (rid,))
    return r["r"]
