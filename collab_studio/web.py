"""FastAPI routes. Pages are server-rendered (views.py); a little vanilla JS adds zoom, previews and progress polling."""
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import views
from .config import PACKAGE_DIR, load_env
from .payments import PaymentError, StripePayments
from .services import STATUS_LABELS, FormErrors, ProofChanged, Studio, UserError
from .storage import UploadError

log = logging.getLogger("collab_studio")
ADMIN_COOKIE, FLASH_COOKIE = "cs_admin", "cs_flash"
SESSION_HOURS = 12


def create_app(env=None, studio: Studio = None, start_worker=None) -> FastAPI:
    env = env or load_env()
    studio = studio or Studio(env)
    run_worker = env.run_worker if start_worker is None else start_worker
    login_attempts: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        if run_worker:
            studio.start_worker()
        if env.admin_password_file:
            log.warning("Admin password is stored in %s (set COLLAB_ADMIN_PASSWORD to choose your own).",
                        env.admin_password_file)
        yield
        studio.stop_worker()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.studio = studio
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    # ── security plumbing ─────────────────────────────────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method == "POST" and not request.url.path.startswith("/webhooks/"):
            src = request.headers.get("origin") or request.headers.get("referer")
            if src and src != "null" and urlparse(src).netloc != request.headers.get("host", ""):
                return Response("Cross-site form posts aren't allowed.", status_code=403)
        resp = await call_next(request)
        if not request.url.path.startswith("/static/"):
            log.info("%s %s %s", request.method, re.sub(r"^/p/[^/]+", "/p/<private>", request.url.path),
                     resp.status_code)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer" if request.url.path.startswith(("/p/", "/admin"))
                                else "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self'; form-action 'self' https://checkout.stripe.com; "
            "frame-ancestors 'none'; base-uri 'none'")
        if request.url.path.startswith(("/p/", "/admin")):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    def sign(value: str) -> str:
        return hmac.new(env.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()

    def admin_session(request: Request):
        c = request.cookies.get(ADMIN_COOKIE, "")
        try:
            exp, nonce, sig = c.split(".")
            if int(exp) > time.time() and hmac.compare_digest(sig, sign(f"{exp}.{nonce}")):
                return c
        except ValueError:
            pass
        return None

    def csrf_for(session: str) -> str:
        return sign("csrf:" + session)[:32]

    def page(html: str, status=200, request: Request = None):
        if request is not None and (s := admin_session(request)):
            html = html.replace(views.CSRF_SLOT, f'<input type="hidden" name="csrf" value="{csrf_for(s)}">')
        return HTMLResponse(html, status_code=status)

    def redirect(url: str, flash: str = "", kind: str = "ok"):
        r = RedirectResponse(url, status_code=303)
        if flash:
            r.set_cookie(FLASH_COOKIE, quote(json.dumps([kind, flash[:400]])), max_age=60, httponly=True,
                         samesite="lax", secure=env.cookie_secure)
        return r

    def pop_flash(request: Request):
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw:
            return "", ""
        try:
            from urllib.parse import unquote
            kind, msg = json.loads(unquote(raw))
            return (msg, "") if kind == "ok" else ("", msg)
        except (ValueError, TypeError):
            return "", ""

    def flashed(resp, request):
        if request.cookies.get(FLASH_COOKIE):
            resp.delete_cookie(FLASH_COOKIE)
        return resp

    def st():
        return studio.settings()

    def private(token):
        req = studio.by_token(token)
        if not req:
            return None, HTMLResponse(views.message_page(st(), "Link not found",
                                                         "This private link isn't valid. Check the latest email we sent you."),
                                      status_code=404)
        return req, None

    async def form_of(request):
        return await request.form()

    # ── public ────────────────────────────────────────────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def home():
        return views.landing(st())

    @app.get("/create", response_class=HTMLResponse)
    def create_form():
        return views.builder(st())

    @app.post("/create")
    async def create_submit(request: Request):
        form = await form_of(request)
        files = []
        for f in form.getlist("references"):
            if hasattr(f, "read") and getattr(f, "filename", ""):
                files.append((f.filename, await f.read(16 * 1024 * 1024)))
        values = {k: form.get(k, "") for k in ("idea", "garment", "style", "colors", "back_name", "back_number",
                                               "instructions", "customer_name", "email", "own_brand")}
        try:
            req, token = studio.create_request(values, files)
        except FormErrors as e:
            return HTMLResponse(views.builder(st(), values, e.errors), status_code=422)
        return redirect(f"/p/{token}", "Thanks! Your idea is in the design queue. We've emailed you this private link.")

    # ── customer private pages ────────────────────────────────────────────────────────────────────────────────────
    @app.get("/p/{token}", response_class=HTMLResponse)
    def review(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        notice, error = pop_flash(request)
        job = studio.active_job(req["id"])
        last = studio.jobs(req["id"])
        if not job and last and last[0]["status"] == "failed" and not studio.versions(req["id"], customer=True):
            job = last[0]
        html = views.review_page(st(), req, token, studio.versions(req["id"], customer=True), job,
                                 studio.revisions(req["id"]), studio.storage.list(req["id"], "reference"),
                                 studio.updates(req["id"]), notice, error)
        return flashed(HTMLResponse(html), request)

    @app.get("/p/{token}/status.json")
    def status_json(token: str):
        req = studio.by_token(token)
        if not req:
            return JSONResponse({"error": "not found"}, status_code=404)
        job = studio.active_job(req["id"]) or (studio.jobs(req["id"]) or [None])[0]
        return {"status": req["status"], "label": STATUS_LABELS.get(req["status"]),
                "job": {"status": job["status"], "progress": job["progress"], "stage": job["stage"]} if job else None}

    @app.post("/p/{token}/choose")
    async def choose(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        form = await form_of(request)
        try:
            studio.choose(req["id"], int(form.get("version_id", 0)))
        except (UserError, ValueError) as e:
            return redirect(f"/p/{token}", str(e), "err")
        req = studio.get_request(req["id"])
        if req["status"] == "final_proof_ready":
            return redirect(f"/p/{token}/approve", "Great choice! Confirm your details to approve the final proof.")
        return redirect(f"/p/{token}", "Great choice! We'll email you when your final proof is ready.")

    @app.post("/p/{token}/revise")
    async def revise(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        form = await form_of(request)
        try:
            studio.request_revision(req["id"], int(form.get("version_id", 0)), form.get("note", ""))
        except (UserError, ValueError) as e:
            return redirect(f"/p/{token}", str(e), "err")
        return redirect(f"/p/{token}", "Got it — we're working on your changes. Your earlier versions are saved below.")

    @app.get("/p/{token}/approve", response_class=HTMLResponse)
    def approve_form(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        if req["status"] in ("preparing_production", "in_production", "shipped"):
            return redirect(f"/p/{token}/order")
        proof = studio.current_proof(req["id"])
        if not proof:
            return redirect(f"/p/{token}", "Your final proof isn't ready yet — we'll email you when it is.", "err")
        notice, error = pop_flash(request)
        html = views.approve_page(st(), req, token, studio.version(req["id"], proof["version_id"]), proof,
                                  studio.current_approval(req["id"]), studio.order(req["id"]), notice, error)
        return flashed(HTMLResponse(html), request)

    @app.post("/p/{token}/approve")
    async def approve(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        f = await form_of(request)
        try:
            studio.approve(req["id"], int(f.get("version_id", 0)), int(f.get("proof_id", 0)), f.get("back_name", ""),
                           f.get("back_number", ""), f.get("size", ""), f.get("quantity", ""), f.get("confirm") == "1")
        except ProofChanged as e:
            return redirect(f"/p/{token}", str(e))
        except (UserError, ValueError) as e:
            return redirect(f"/p/{token}/approve", str(e), "err")
        return redirect(f"/p/{token}/approve", "Approved. Review your total and pay when you're ready.")

    @app.post("/p/{token}/checkout")
    def checkout(token: str):
        req, err = private(token)
        if err:
            return err
        try:
            url = studio.start_checkout(req["id"])
        except (UserError, PaymentError) as e:
            return redirect(f"/p/{token}/approve", str(e) if isinstance(e, UserError) else
                            "We couldn't open checkout just now. Please try again in a minute.", "err")
        return RedirectResponse(url, status_code=303)

    @app.get("/p/{token}/test-checkout", response_class=HTMLResponse)
    def test_checkout(token: str, session: str = ""):
        req, err = private(token)
        if err:
            return err
        chk = studio.db.one("SELECT * FROM checkouts WHERE id=?", (session,))
        o = studio.order(req["id"])
        if studio.payments.name != "simulated" or not chk or not o or chk["order_id"] != o["id"]:
            return HTMLResponse(views.message_page(st(), "Checkout not found", "Start checkout from your proof page."),
                                status_code=404)
        return views.test_checkout_page(st(), req, token, o, session)

    @app.post("/p/{token}/test-checkout")
    async def test_checkout_pay(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        f = await form_of(request)
        sid = f.get("session", "")
        chk = studio.db.one("SELECT * FROM checkouts WHERE id=?", (sid,))
        o = studio.order(req["id"])
        if studio.payments.name != "simulated" or not chk or not o or chk["order_id"] != o["id"]:
            return Response("Unknown checkout", status_code=404)
        ship = {k: (f.get(k) or "")[:120] for k in ("first_name", "last_name", "address1", "city", "zip", "region",
                                                    "country")}
        ship["email"] = req["email"]
        outcome = studio.confirm_checkout(sid, shipping=ship)
        msg = {"paid": "Payment confirmed (test mode). Thank you!", "duplicate": "This payment was already recorded.",
               "already_paid": "This order is already paid."}.get(outcome, "We couldn't confirm that payment.")
        return redirect(f"/p/{token}/order", msg, "ok" if outcome in ("paid", "duplicate", "already_paid") else "err")

    @app.get("/p/{token}/paid")
    def stripe_return(token: str, session_id: str = ""):
        req, err = private(token)
        if err:
            return err
        if not isinstance(studio.payments, StripePayments):
            return redirect(f"/p/{token}/order")
        chk = studio.db.one("SELECT * FROM checkouts WHERE id=?", (session_id,))
        o = studio.order(req["id"])
        if not chk or not o or chk["order_id"] != o["id"]:
            return redirect(f"/p/{token}/approve", "We couldn't find that checkout.", "err")
        try:
            view = studio.payments.retrieve(session_id)
        except PaymentError:
            return redirect(f"/p/{token}/order", "We're confirming your payment — this page will update shortly.")
        outcome = studio.confirm_checkout(session_id, provider_view=view, shipping=view["shipping"])
        if outcome in ("paid", "duplicate", "already_paid"):
            return redirect(f"/p/{token}/order", "Payment confirmed. Thank you!")
        return redirect(f"/p/{token}/order", "We haven't received confirmation of your payment yet. We'll email you "
                                             "as soon as it arrives.")

    @app.get("/p/{token}/order", response_class=HTMLResponse)
    def order_view(token: str, request: Request):
        req, err = private(token)
        if err:
            return err
        notice, _ = pop_flash(request)
        o = studio.order(req["id"])
        a = None
        if o:
            a = studio.db.one("SELECT * FROM approvals WHERE id=?", (o["paid_approval_id"] or o["approval_id"],))
        v = studio.version(req["id"], a["version_id"]) if a else (
            studio.version(req["id"], req["chosen_version_id"]) if req["chosen_version_id"] else None)
        html = views.order_page(st(), req, token, o, a, v, studio.updates(req["id"]), notice)
        return flashed(HTMLResponse(html), request)

    @app.get("/p/{token}/asset/{asset_id}")
    def customer_asset(token: str, asset_id: int):
        req = studio.by_token(token)
        if not req or not studio.customer_can_see(req["id"], asset_id):
            return Response("Not found", status_code=404)
        a = studio.storage.get(asset_id)
        return FileResponse(studio.storage.path_of(a), media_type=a["mime"],
                            headers={"Cache-Control": "private, max-age=3600"})

    # ── webhooks ──────────────────────────────────────────────────────────────────────────────────────────────────
    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        if not isinstance(studio.payments, StripePayments):
            return Response(status_code=404)
        payload = await request.body()
        try:
            studio.payments.verify_webhook(payload, request.headers.get("stripe-signature", ""))
        except PaymentError:
            return Response("bad signature", status_code=400)
        evt = json.loads(payload)
        if evt.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            obj = evt["data"]["object"]
            view = StripePayments.view(obj)
            outcome = studio.confirm_checkout(obj["id"], event_id=evt["id"], provider_view=view,
                                              shipping=view["shipping"])
            return {"received": True, "outcome": outcome}
        return {"received": True}

    # ── admin ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def require_admin(request: Request):
        return admin_session(request)

    async def admin_form(request: Request):
        s = admin_session(request)
        if not s:
            return None, redirect("/admin/login")
        f = await request.form()
        if not hmac.compare_digest(f.get("csrf", ""), csrf_for(s)):
            return None, Response("Form expired — go back and reload the page.", status_code=403)
        return f, None

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_form():
        return views.admin_login(st())

    @app.post("/admin/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        recent = [t for t in login_attempts.get(ip, []) if t > time.time() - 600]
        if len(recent) >= 10:
            return HTMLResponse(views.admin_login(st(), "Too many attempts. Try again in a few minutes."), status_code=429)
        f = await request.form()
        if not hmac.compare_digest(str(f.get("password", "")).encode(), env.admin_password.encode()):
            login_attempts[ip] = recent + [time.time()]
            return HTMLResponse(views.admin_login(st(), "Wrong password."), status_code=401)
        login_attempts.pop(ip, None)
        exp = str(int(time.time() + SESSION_HOURS * 3600))
        nonce = secrets.token_hex(8)
        r = RedirectResponse("/admin", status_code=303)
        r.set_cookie(ADMIN_COOKIE, f"{exp}.{nonce}.{sign(f'{exp}.{nonce}')}", httponly=True, samesite="strict",
                     secure=env.cookie_secure, max_age=SESSION_HOURS * 3600)
        return r

    @app.post("/admin/logout")
    async def logout(request: Request):
        r = redirect("/admin/login")
        r.delete_cookie(ADMIN_COOKIE)
        return r

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request, status: str = ""):
        if not require_admin(request):
            return redirect("/admin/login")
        rows = studio.db.all("SELECT * FROM requests ORDER BY updated_at DESC LIMIT 500")
        for r in rows:
            r["flags"] = []
            if studio.db.one("SELECT 1 FROM design_versions WHERE request_id=? AND state='draft'", (r["id"],)):
                r["flags"].append("concepts to review")
            if studio.db.one("SELECT 1 FROM jobs WHERE request_id=? AND status='failed' AND id=(SELECT MAX(id) FROM "
                             "jobs WHERE request_id=?)", (r["id"], r["id"])):
                r["flags"].append("generation failed")
            if r["branding_status"] == "pending":
                r["flags"].append("branding review")
            if r["status"] == "preparing_production":
                r["flags"].append("ready for production")
        if status == "attention":
            rows = [r for r in rows if r["flags"]]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        return page(views.admin_list(st(), rows, status), request=request)

    def detail_data(rid):
        req = studio.get_request(rid)
        return {
            "req": req, "refs": studio.storage.list(rid, "reference"), "templates": studio.storage.list(rid, "template"),
            "brief": studio.latest_brief(rid), "jobs": studio.jobs(rid), "versions": studio.versions(rid),
            "revisions": studio.revisions(rid), "proof": studio.current_proof(rid),
            "approval": studio.current_approval(rid),
            "old_approvals": studio.db.all("SELECT * FROM approvals WHERE request_id=? AND valid=0 ORDER BY id DESC",
                                           (rid,)),
            "order": studio.order(rid), "artwork_report": studio.validate_artwork(rid),
            "blockers": studio.production_blockers(rid), "updates": studio.updates(rid, customer=False),
            "emails": studio.db.all("SELECT id, subject, status, created_at FROM emails WHERE request_id=? ORDER BY id "
                                    "DESC", (rid,)),
            "provider": f"{studio.provider.name}{' (demo)' if studio.provider.is_demo else ''}",
        }

    @app.get("/admin/r/{rid}", response_class=HTMLResponse)
    def admin_detail(rid: int, request: Request):
        if not require_admin(request):
            return redirect("/admin/login")
        if not studio.get_request(rid):
            return Response("Not found", status_code=404)
        notice, error = pop_flash(request)
        return flashed(page(views.admin_detail(st(), detail_data(rid), notice, error), request=request), request)

    async def file_bytes(f, key):
        up = f.get(key)
        if not up or not hasattr(up, "read") or not getattr(up, "filename", ""):
            raise UserError(f"Choose a file for {key}.")
        return up.filename, await up.read(16 * 1024 * 1024)

    @app.post("/admin/r/{rid}/{action}")
    async def admin_action(rid: int, action: str, request: Request):
        f, err = await admin_form(request)
        if err:
            return err
        if not studio.get_request(rid):
            return Response("Not found", status_code=404)
        back = f"/admin/r/{rid}"
        try:
            msg = "Saved."
            if action == "regenerate":
                studio.regenerate(rid, f.get("admin_instructions"))
                msg = "Generating two new concepts."
            elif action == "send":
                studio.send_versions(rid, [int(x) for x in f.getlist("version_ids")])
                msg = "Sent to the customer."
            elif action == "hold":
                studio.hold_version(rid, int(f.get("version_id", 0)))
            elif action == "upload-concept":
                _, fb = await file_bytes(f, "front")
                _, bb = await file_bytes(f, "back")
                studio.upload_concept(rid, f.get("label", ""), fb, bb, f.get("note", ""))
                msg = "Concept uploaded as a draft."
            elif action == "upload-proof":
                _, fb = await file_bytes(f, "front")
                _, bb = await file_bytes(f, "back")
                studio.upload_proof(rid, fb, bb)
                msg = "Proof uploaded; the customer has been asked to approve it."
            elif action == "personalisation":
                studio.set_personalisation(rid, f.get("back_name", ""), f.get("back_number", ""))
            elif action == "branding":
                studio.set_branding(rid, f.get("status", ""), f.get("notes", ""))
            elif action == "grant-revision":
                studio.grant_revision(rid)
            elif action == "artwork":
                name, data = await file_bytes(f, "file")
                studio.upload_artwork(rid, f.get("panel", ""), data, name)
                msg = "Panel uploaded — review it before production."
            elif action == "template":
                name, data = await file_bytes(f, "file")
                studio.storage.save(rid, "template", data, original_name=name, allow_pdf=True)
            elif action == "artwork-review":
                studio.set_artwork_review(rid, f.get("passed") == "1", f.get("notes", ""))
            elif action == "submit":
                res = studio.submit_to_production(rid, f.get("external_ref", ""))
                msg = f"Submitted ({res['ref']}). {res['note']}"
            elif action == "cost":
                studio.record_cost(rid, f.get("amount", ""))
            elif action == "tracking":
                studio.add_tracking(rid, f.get("carrier", ""), f.get("number", ""), f.get("url", ""))
                msg = "Tracking saved and the customer was emailed."
            elif action == "update":
                studio.post_update(rid, f.get("message", ""))
            elif action == "rotate-link":
                studio.rotate_link(rid)
                msg = "A new private link was emailed; the old one no longer works."
            else:
                return Response("Unknown action", status_code=404)
        except (UserError, UploadError, ValueError) as e:
            return redirect(back, str(e), "err")
        return redirect(back, msg)

    @app.get("/admin/asset/{asset_id}")
    def admin_asset(asset_id: int, request: Request):
        if not require_admin(request):
            return Response(status_code=401)
        a = studio.storage.get(asset_id)
        if not a:
            return Response(status_code=404)
        headers = {"Cache-Control": "private, no-store"}
        if not a["mime"].startswith("image/"):
            headers["Content-Disposition"] = "attachment"
        return FileResponse(studio.storage.path_of(a), media_type=a["mime"], headers=headers)

    @app.get("/admin/r/{rid}/pack.zip")
    def production_pack(rid: int, request: Request):
        if not require_admin(request):
            return Response(status_code=401)
        req = studio.get_request(rid)
        if not req:
            return Response(status_code=404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for panel, a in studio.latest_artwork(rid).items():
                z.writestr(f"artwork/{panel}.{a['path'].rsplit('.', 1)[-1]}", studio.read_asset(a))
            p = studio.current_proof(rid)
            if p:
                z.writestr("proof/front.png", studio.read(p["front_asset_id"]))
                z.writestr("proof/back.png", studio.read(p["back_asset_id"]))
            o, a = studio.order(rid), studio.current_approval(rid)
            z.writestr("order.json", json.dumps({
                "ref": req["ref"], "garment": req["garment"], "approval": a,
                "shipping": o["shipping"] if o else None, "artwork_status": o["artwork_status"] if o else None,
            }, indent=2, default=str))
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{req["ref"]}-production.zip"'})

    @app.get("/admin/settings", response_class=HTMLResponse)
    def settings_form(request: Request):
        if not require_admin(request):
            return redirect("/admin/login")
        notice, error = pop_flash(request)
        return flashed(page(views.admin_settings(st(), notice, error), request=request), request)

    @app.post("/admin/settings")
    async def settings_save(request: Request):
        f, err = await admin_form(request)
        if err:
            return err
        try:
            ch = {}
            for k in ("business_name", "support_email", "currency", "tax_note", "turnaround_design",
                      "turnaround_production"):
                ch[k] = str(f.get(k, "")).strip()
            for k in ("revision_limit", "max_generations_per_request", "generation_max_attempts"):
                ch[k] = max(0, int(f.get(k, 0)))
            ch["generation_max_attempts"] = max(1, ch["generation_max_attempts"])
            tr = str(f.get("tax_rate_percent", "")).strip()
            ch["tax_rate_percent"] = float(tr) if tr else None
            for k in ("prices_are_demo", "require_admin_review", "require_admin_proof"):
                ch[k] = f.get(k) == "1"
            for k in ("garments", "shop_links", "faq", "ship_countries"):
                ch[k] = json.loads(f.get(k, ""))
            for k, g in ch["garments"].items():
                if not {"label", "price", "shipping", "sizes"} <= set(g):
                    raise UserError(f"Garment {k} needs label, price, shipping and sizes.")
                float(g["price"]), float(g["shipping"])
            studio.update_settings(ch)
        except (ValueError, UserError) as e:
            return redirect("/admin/settings", f"Not saved: {e}", "err")
        return redirect("/admin/settings", "Settings saved.")

    @app.get("/admin/emails", response_class=HTMLResponse)
    def emails(request: Request):
        if not require_admin(request):
            return redirect("/admin/login")
        rows = studio.db.all("SELECT * FROM emails ORDER BY id DESC LIMIT 200")
        return page(views.admin_emails(st(), rows, env.email_provider), request=request)

    @app.get("/admin/emails/{eid}")
    def email_preview(eid: int, request: Request):
        if not require_admin(request):
            return redirect("/admin/login")
        e = studio.db.one("SELECT * FROM emails WHERE id=?", (eid,))
        if not e:
            return Response(status_code=404)
        return HTMLResponse(e["html"], headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})

    return app
