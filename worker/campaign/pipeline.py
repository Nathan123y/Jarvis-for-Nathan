"""The campaign jobs. Each step is its own queued job, so a crash or a restart loses at most one step.

    campaign_discover -> campaign_audit (verify, find the real site, audit, qualify, research)
      -> generate_site (build + objective checks; one at a time) -> campaign_publish (reachable preview)
      -> campaign_offer (compose + validate + queue in the outbox) -> campaign_send (limits, then Gmail)
      -> campaign_replies (classify, suppress opt-outs, stop on complaints/bounces)

Modes: research stops after the audit and research step; draft goes on to build, check, publish and
write the messages but queues nothing; autonomous (needs a live authorization, policy.py) queues and
sends within the policy's limits. Every step re-checks the kill switch, and every external action
re-checks the authorization, limits, suppression list and the message itself.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.events import Store as EventStore
from worker.campaign import audit as auditmod, build, check, discover, mail as mailmod, messages, policy as pol, preview, replies
from worker.campaign.policy import Policy
from worker.campaign.store import CampaignStore
from worker.runtime import Deferred, Fatal, NeedsSetup

SEND_PER_RUN = 5
PROMOTION_SOURCE = "promotion"


@dataclass
class Env:
    store: CampaignStore
    provider: object
    fetcher: object
    host: object
    mailer: object
    identity: Callable[[], dict]
    workdir: Path
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    pace: float = 30.0                         # seconds between two sends


def default_env() -> Env:
    from memory.config_manager import get_plugin_config
    from worker.runtime import default_dir
    d = default_dir()
    cfg = get_plugin_config("website_campaign")

    def identity() -> dict:
        c = get_plugin_config("product_sales")
        return {"sender_name": (c.get("sender_name") or "").strip(), "postal_address": (c.get("postal_address") or "").strip(),
                "gmail_account": (c.get("gmail_account") or "spam").strip() or "spam"}
    return Env(store=CampaignStore(d / "campaign.db"), provider=discover.OverpassProvider(), fetcher=auditmod.HttpFetcher(),
               host=preview.from_settings(cfg, d), mailer=mailmod.GmailMailer("spam"), identity=identity, workdir=d)


# ── helpers ──────────────────────────────────────────────────────────────────
def _policy(env: Env, cid: str) -> tuple[dict, Policy]:
    c = env.store.campaign(cid)
    if c is None:
        raise Fatal(f"unknown campaign {cid}")
    return c, pol.from_dict(c["policy"])


def _emit(ctx, kind: str, cid: str, biz: dict, *, status: str = "", detail: Optional[dict] = None,
          evidence: Optional[dict] = None, source_suffix: str = "", ts: Optional[float] = None, private: bool = True) -> None:
    ctx.events.record(kind, source="worker", source_id=f"{kind}:{cid}:{biz['id']}{source_suffix}", ts=ts,
                      task_id=str(biz["id"]), campaign_id=cid, status=status, title=messages.clean(biz["name"], 60),
                      detail=detail or {}, evidence=evidence or {}, private=private)


def _active(env: Env, cid: str, ctx) -> tuple[Optional[dict], Optional[Policy], str]:
    """(campaign, policy, why-not). A paused or stopped campaign quietly skips its jobs."""
    c, p = _policy(env, cid)
    if c["status"] != "active":
        return None, None, f"campaign is {c['status']}" + (f" ({c['stop_reason']})" if c["stop_reason"] else "")
    return c, p, ""


def _qualified_count(env: Env, cid: str) -> int:
    return sum(1 for b in env.store.businesses(cid) if b["status"] == "ok" and b["stage"] not in ("found", "verified", "audited"))


FREEMAIL = discover.FREE_MAIL
ROLE_PREFIXES = ("abuse", "postmaster", "noreply", "no-reply", "donotreply", "privacy", "legal", "webmaster", "mailer-daemon", "security")
RECHECK_AFTER = 30 * 60            # an uncertain send is only looked up once Gmail's index has had time to catch up


def suppression_keys(email: str, website: str = "") -> list[str]:
    """What an opt-out blocks: the address, and the business's own domain (never a shared mail domain
    like gmail.com, which would block unrelated businesses)."""
    email = (email or "").strip().lower()
    dom = email.split("@")[-1] if "@" in email else ""
    keys = [email]
    if dom and dom not in FREEMAIL:
        keys.append(dom)
    if website:
        host = discover.host_of(website if "//" in website else "//" + website)
        if host and host not in FREEMAIL:
            keys.append(host)
    return [k for k in keys if k]


def sent_today_all(env: Env, ctx, now: float) -> int:
    """Sends (and possible sends) since local midnight across ALL campaigns, including the older
    product-promotion emails, so the daily cap is a real cap."""
    since = pol.local_midnight(now)
    n = ctx.db.outbox_count_sent_since(since)
    n += len([e for e in ctx.events.events(kinds=("offer_sent",), since=since)
              if e["source"] == PROMOTION_SOURCE and e["status"] in ("sent", "uncertain")])
    return n


# ── discover ─────────────────────────────────────────────────────────────────
def h_discover(ctx, job, env: Env) -> dict:
    cid = job["payload"]["campaign_id"]
    c, p, why = _active(env, cid, ctx)
    if not c:
        return {"skipped": why}
    cats = [x for x in p.categories if x in discover.CATEGORY_TAGS] or list(discover.CATEGORY_TAGS)[:6]
    dedupe = discover.Deduper.from_store(env.store)
    found: list = []
    for area in [p.area, *p.nearby_areas]:
        if ctx.should_stop():
            return ctx.partial({"found": len(found)})
        try:
            found += env.provider.search(area, cats)
        except Exception as exc:
            raise RuntimeError(f"business search failed for {area}: {type(exc).__name__}") from exc
    already = _qualified_count(env, cid)
    room = max(0, p.batch_size - already)
    new = discover.select_candidates(found, dedupe, limit=max(room * 4, 0))
    made = 0
    for b in new:
        b["data"] = {**(b.get("data") or {}), "keys": discover.keys_for(b)}
        keys = discover.keys_for(b)
        bid, created = env.store.add_business(cid, keys[0] if keys else f"x:{b['source_id']}", b)
        if not created:
            continue
        made += 1
        biz = env.store.business(bid)
        _emit(ctx, "biz_found", cid, biz, evidence={"source": b["source"], "url": b["data"].get("osm_url"),
                                                      "retrieved_at": b["data"].get("retrieved_at")})
        ctx.db.enqueue("campaign_audit", {"business_id": bid}, campaign_id=cid, unique_key=f"audit:{bid}",
                       max_attempts=2, timeout_s=240)
    env.store.audit(cid, "worker", "discovered", {"candidates": len(found), "new": made, "room": room})
    return {"candidates": len(found), "new": made}


# ── audit + qualify + research ───────────────────────────────────────────────
def _facts(biz: dict, site_audit: Optional[auditmod.Audit], now: float) -> tuple[list, list]:
    """Facts with source and date; conflicts between sources."""
    d = biz["data"]
    src, when = d.get("osm_url", ""), d.get("retrieved_at", now)
    facts = [{"field": f, "value": biz[f], "url": src, "date": when} for f in ("name", "address", "phone", "email") if biz.get(f)]
    if d.get("hours"):
        facts.append({"field": "hours", "value": d["hours"], "url": src, "date": when})
    conflicts = []
    if site_audit and site_audit.reachable:
        sp = site_audit.facts.get("phones") or []
        mine = discover.digits(biz.get("phone", ""))
        if mine and sp and mine not in sp:
            conflicts.append({"field": "phone", "listing": biz["phone"], "website": sp[0], "url": site_audit.pages[0]})
    return facts, conflicts


def h_audit(ctx, job, env: Env) -> dict:
    bid = job["payload"]["business_id"]
    biz = env.store.business(bid)
    if biz is None or biz["status"] != "ok" or biz["stage"] != "found":
        return {"skipped": "already handled"}
    cid = biz["campaign_id"]
    c, p, why = _active(env, cid, ctx)
    if not c:
        return {"skipped": why}
    now = env.clock()

    def reject(reason: str) -> dict:
        env.store.update_business(bid, status="rejected", hold_reason=reason, now=now)
        return {"rejected": reason}

    # 1. verify the listing is a real, locatable business
    if not biz["name"] or not biz["category"] or not (biz["address"] or biz["phone"]):
        return reject("listing lacks a category and an address or phone, so it can't be verified")
    # 2. already opted out / contacted?
    if env.store.is_suppressed(*suppression_keys(biz["email"], biz["website"])):
        return reject("on the do-not-contact list")
    # 3. find the actual website, beyond the listing's website field
    checks = ["its OpenStreetMap listing"]
    site_url = biz["website"] if biz["website"] and not discover.is_social_or_directory(biz["website"]) else ""
    if biz["website"] and not site_url:
        checks.append("its listed social/directory page (not a website)")
    probe = auditmod.candidate_domain(biz["email"]) if not site_url else ""
    result: Optional[auditmod.Audit] = None
    if site_url:
        result = auditmod.audit_site(site_url if "//" in site_url else "https://" + site_url, env.fetcher, clock=env.clock, pause=env.sleep)
    elif probe:
        checks.append(f"the website at its email domain ({probe})")
        trial = auditmod.audit_site("https://" + probe, env.fetcher, clock=env.clock, pause=lambda s: None)
        if trial.skipped:
            env.store.update_business(bid, stage="audited", status="held", hold_reason="its site asks not to be audited by automated tools, so I can't say whether it has a website", now=now)
            return {"held": "site not auditable"}
        if trial.reachable:
            page = env.fetcher.get(trial.pages[0]) if trial.pages else None
            text = auditmod.parse(page.html)["text"] if page else ""
            if auditmod.same_business(trial.facts.get("phones", []), text, biz):
                result, site_url = trial, "https://" + probe
    if result and result.skipped:
        return reject(f"its site asks not to be audited by automated tools ({result.skipped})")
    problems = result.problems if result else []
    if result and result.reachable and not problems:
        return reject("its website has no observable problems")
    # 4. a published contact is required
    email = biz["email"]
    email_url = biz["data"].get("osm_url", "")
    if not email and result:
        for e in sorted(result.emails, key=lambda x: x["email"]):
            if e["email"].split("@")[0].lower().startswith(ROLE_PREFIXES):
                continue
            if discover.host_of("//" + e["email"].split("@")[-1]) == discover.host_of(site_url):
                email, email_url = e["email"], e["url"]          # a contact the business published on its own site
                break
    audit_blob = {"site": site_url, "problems": problems, "no_website_checks": checks if not site_url else [],
                  "pages": result.pages if result else [], "checked_at": result.checked_at if result else now}
    facts, conflicts = _facts({**biz, "email": email}, result, now)
    base = {"audit": audit_blob, "facts": facts, "conflicts": conflicts, "email_source": email_url}
    if not email:
        env.store.update_business(bid, stage="audited", status="held", hold_reason="no published contact email", data=base, now=now)
        return {"held": "no published contact email"}
    if env.store.is_suppressed(*suppression_keys(email, site_url)):
        return reject("contact is on the do-not-contact list")
    if env.store.email_in_use(email, exclude_id=bid):
        return reject("another listing is already being pitched at this contact address")
    if _qualified_count(env, cid) >= p.batch_size:
        env.store.update_business(bid, status="backlog", hold_reason="batch is full", data=base, now=now)
        return {"backlog": True}
    env.store.update_business(bid, stage="qualified", email=email, website=site_url or biz["website"], data=base, now=now)
    biz = env.store.business(bid)
    _emit(ctx, "biz_qualified", cid, biz, detail={"basis": problems[0]["code"] if problems else "no_website_verified",
                                                   "conflicts": len(conflicts)},
          evidence={"problems": problems[:3], "checked": checks, "email_source": email_url})
    if c["mode"] == "research":
        return {"qualified": True}
    ctx.db.enqueue("generate_site", {"business_id": bid}, campaign_id=cid, unique_key=f"gen:{bid}", max_attempts=2, timeout_s=300)
    return {"qualified": True}


# ── build + check ────────────────────────────────────────────────────────────
def h_generate(ctx, job, env: Env) -> dict:
    bid = job["payload"]["business_id"]
    biz = env.store.business(bid)
    if biz is None or biz["status"] != "ok" or biz["stage"] != "qualified":
        return {"skipped": "already handled"}
    cid = biz["campaign_id"]
    c, p, why = _active(env, cid, ctx)
    if not c:
        return {"skipped": why}
    if c["mode"] == "research":
        return {"skipped": "research-only campaign"}
    ident = env.identity()
    if not ident.get("sender_name"):
        raise NeedsSetup("Set your sender name in Plugin Settings (Product promotion) so concept pages can say who prepared them")
    now = env.clock()
    conflicted = {x["field"] for x in biz["data"].get("conflicts", [])}
    shown = {k: ("" if k in conflicted else biz[k]) for k in ("phone", "email", "address", "city", "name", "category")}
    site = build.render(shown, {"hours": biz["data"].get("hours", "")}, sender=ident["sender_name"])
    static = check.static_checks(site.files["index.html"], site.theme, site.used, ident["sender_name"])
    out = env.workdir / "sites" / str(bid)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(site.files["index.html"], encoding="utf-8")
    visual = check.visual_check(out / "index.html", out) if not ctx.should_stop() else None
    ok, why_not = check.verdict(static, visual, p.require_visual_check)
    data = {"site": {"theme": site.theme, "layout": site.layout, "used": site.used},
            "checks": {"static": static, "visual": visual, "passed": ok, "why_not": why_not}}
    biz = {**biz, "id": bid}
    _emit(ctx, "site_built", cid, biz, detail={"theme": site.theme}, evidence={"files": ["index.html"]})
    if not ok:
        env.store.update_business(bid, stage="built", status="held", hold_reason=why_not[:250], data=data, now=now)
        return {"held": why_not}
    env.store.update_business(bid, stage="checked", data=data, now=now)
    _emit(ctx, "site_checked", cid, biz, status="passed",
          detail={"static": f"{sum(1 for r in static if r['ok'])}/{len(static)}", "visual": (visual or {}).get("status", "not_run")},
          evidence={"screens": (visual or {}).get("screens", [])})
    ctx.db.enqueue("campaign_publish", {"business_id": bid}, campaign_id=cid, unique_key=f"pub:{bid}", max_attempts=4, timeout_s=600)
    return {"checked": True}


# ── publish a reachable preview ──────────────────────────────────────────────
def h_publish(ctx, job, env: Env) -> dict:
    bid = job["payload"]["business_id"]
    biz = env.store.business(bid)
    if biz is None or biz["status"] != "ok" or biz["stage"] != "checked":
        return {"skipped": "already handled"}
    cid = biz["campaign_id"]
    c, p, why = _active(env, cid, ctx)
    if not c:
        return {"skipped": why}
    if not env.host.public:
        raise NeedsSetup("Preview hosting isn't set up, so websites are built but nothing can be sent (see docs/while-you-were-away.md)")
    if not ctx.external_allowed():
        return ctx.partial()
    slug = biz["data"].get("slug") or preview.new_slug()
    env.store.update_business(bid, data={"slug": slug})
    files = {"index.html": (env.workdir / "sites" / str(bid) / "index.html").read_text(encoding="utf-8")}
    url = env.host.publish(slug, files)
    ok, detail = env.host.verify(url, slug)
    if not ok:
        raise RuntimeError(f"preview isn't reachable yet: {detail}")
    now = env.clock()
    env.store.update_business(bid, stage="preview", data={"preview_url": url, "preview_verified_at": now}, now=now)
    _emit(ctx, "preview_published", cid, {**biz, "id": bid}, status="live", detail={"url": url},
          evidence={"url": url, "verified": detail, "verified_at": now})
    ctx.db.enqueue("campaign_offer", {"business_id": bid}, campaign_id=cid, unique_key=f"offer:{bid}", max_attempts=2)
    return {"preview": url}


# ── compose + queue the offer ────────────────────────────────────────────────
def h_offer(ctx, job, env: Env) -> dict:
    bid = job["payload"]["business_id"]
    biz = env.store.business(bid)
    if biz is None or biz["status"] != "ok" or biz["stage"] != "preview":
        return {"skipped": "already handled"}
    cid = biz["campaign_id"]
    c, p, why = _active(env, cid, ctx)
    if not c:
        return {"skipped": why}
    ident = env.identity()
    missing = [k for k in ("sender_name", "postal_address") if not ident.get(k)]
    if missing:
        raise NeedsSetup("Set your " + " and ".join(m.replace("_", " ") for m in missing) + " in Plugin Settings (Product promotion): commercial email needs both")
    audit_blob = biz["data"]["audit"]
    problems = audit_blob.get("problems", [])
    if problems and audit_blob.get("site"):      # the site may have been fixed since: look again before saying so
        again = auditmod.audit_site(audit_blob["site"] if "//" in audit_blob["site"] else "https://" + audit_blob["site"],
                                    env.fetcher, clock=env.clock, pause=lambda s: None)
        if again.reachable and not again.problems:
            env.store.update_business(bid, status="rejected", hold_reason="its site no longer shows the problem we found", now=env.clock())
            return {"rejected": "problem fixed"}
        problems = again.problems or problems
    url = biz["data"]["preview_url"]
    msg = messages.compose(biz, problems, audit_blob.get("no_website_checks", []), url, p, ident)
    bad = messages.validate(msg, biz, p, ident, url)
    now = env.clock()
    if bad:
        env.store.update_business(bid, status="held", hold_reason="message failed checks: " + "; ".join(bad)[:200], now=now)
        return {"held": bad}
    offer = {"subject": msg["subject"], "body": msg["body"], "basis": msg["basis"], "price_cents": p.price_cents,
             "drafted_at": now}
    if c["mode"] != "autonomous":
        env.store.update_business(bid, data={"offer": {**offer, "state": "drafted"}}, now=now)
        return {"drafted": True}
    auth = pol.authorization(env.store, cid, now)
    if auth["state"] != "active":
        env.store.update_business(bid, data={"offer": {**offer, "state": "drafted"}}, now=now)
        return {"drafted": True, "authorization": auth["reason"]}
    key = f"offer:{cid}:{bid}"
    oid, created = ctx.db.outbox_add(key, {"to": biz["email"], "subject": msg["subject"], "body": msg["body"],
                                           "business_id": bid}, campaign_id=cid, recipient_key=biz["email"].lower(), now=now)
    env.store.update_business(bid, stage="queued", data={"offer": {**offer, "state": "queued", "outbox_id": oid}}, now=now)
    env.store.audit(cid, "worker", "offer_queued", {"business_id": bid, "outbox_id": oid})
    return {"queued": oid}


# ── send (limits first) ──────────────────────────────────────────────────────
def _stop(ctx, env: Env, cid: str, why: str) -> None:
    pol.stop(env.store, cid, why, now=env.clock())
    ctx.events.record("campaign_stopped", source="worker", source_id=f"stop:{cid}:{int(env.clock())}", ts=env.clock(),
                      campaign_id=cid, status="open", title=f"Campaign stopped: {why}"[:200], detail={"reason": why})


def reconcile_uncertain(ctx, env: Env, cid: str) -> int:
    """Settle sends whose outcome was unknown. Only looked up once Gmail has had time to index them,
    and never requeued unless Gmail's search worked and covered the whole period."""
    n = 0
    now = env.clock()
    for row in ctx.db.outbox_list(["uncertain"], campaign_id=cid):
        if now - float(row.get("updated") or row.get("attempted_at") or 0) < RECHECK_AFTER:
            continue
        outcome = ctx.db.reconcile(row["id"], env.mailer.find_sent, now=now)
        bid = row["payload"].get("business_id")
        biz = env.store.business(bid) if bid else None
        if outcome == "sent" and biz:
            mine = ctx.db.outbox_get(row["id"]) or {}
            env.store.update_business(bid, stage="sent", data={"offer": {**biz["data"].get("offer", {}), "state": "sent",
                                      "message_id": mine.get("provider_message_id"), "thread_id": mine.get("provider_thread_id")}}, now=now)
            _emit(ctx, "offer_sent", cid, biz, status="sent", ts=now, source_suffix="",
                  evidence={"message_id": mine.get("provider_message_id"), "thread_id": mine.get("provider_thread_id"), "via": "reconciled with Gmail"})
            n += 1
        elif outcome == "held" and biz:
            ctx.events.record("decision_needed", source="worker", source_id=f"held:{row['id']}", ts=now, task_id=str(bid),
                              campaign_id=cid, status="open", title=f"Couldn't confirm whether the offer to {messages.clean(biz['name'], 40)} was sent",
                              detail={"outbox_id": row["id"]})
    return n


def _gmail_trouble(ctx, env: Env, cid: str, exc, now: float):
    """A sign-in or limit problem stops the campaign; a network blip just waits and tries again."""
    if exc.kind in ("auth", "restricted"):
        _stop(ctx, env, cid, f"Gmail sign-in problem ({exc.detail or exc.kind})" if exc.kind == "auth" else "Gmail limited or blocked sending")
        raise NeedsSetup("Reconnect the spam Gmail account in Jarvis, then re-authorize the campaign")
    raise Deferred(now + 900, f"Gmail not reachable right now ({exc.kind})")


def h_send(ctx, job, env: Env) -> dict:
    sent = 0
    for c in env.store.campaigns():
        cid = c["id"]
        if c["status"] != "active" or c["mode"] != "autonomous":
            continue
        _, p = _policy(env, cid)
        reconcile_uncertain(ctx, env, cid)
        for _ in range(SEND_PER_RUN):
            if ctx.should_stop() or not ctx.external_allowed():
                return ctx.partial({"sent": sent})
            now = env.clock()
            ok, why = pol.may_send(env.store, cid, sent_today_all=sent_today_all(env, ctx, now), now=now)
            if not ok:
                if "hours" in why:
                    raise Deferred(pol.next_window_start(p, now), why)
                break
            ident = env.identity()
            try:
                env.mailer.check()
            except mailmod.MailError as exc:
                _gmail_trouble(ctx, env, cid, exc, now)
            row = ctx.worker.outbox_claim(cid)
            if row is None:
                break
            pl, bid = row["payload"], row["payload"].get("business_id")
            biz = env.store.business(bid) if bid else None
            cur = env.store.campaign(cid) or {}
            reasons = []
            if biz is None or biz["status"] != "ok":
                reasons.append("business is no longer eligible")
            if float(row.get("created") or 0) < float(cur.get("authorized_at") or 0):
                reasons.append("it was queued before the latest authorization; review it again")
            if env.store.is_suppressed(*suppression_keys(pl.get("to", ""), (biz or {}).get("website", ""))):
                reasons.append("recipient is suppressed")
            if biz:
                if env.store.email_in_use(pl.get("to", ""), exclude_id=bid, stages=("sent",)):
                    reasons.append("another business at this address was already emailed")
                verified = float(biz["data"].get("preview_verified_at") or 0)
                if now - verified > (p.preview_ttl_days - 1) * 86400:
                    reasons.append("its preview is too old to still be up")
                reasons += messages.validate({"subject": pl["subject"], "body": pl["body"], "basis": biz["data"].get("offer", {}).get("basis", "x")},
                                             biz, p, ident, biz["data"].get("preview_url", ""))
            if reasons:
                ctx.db.outbox_hold(row["id"], "; ".join(reasons)[:250], now)
                env.store.audit(cid, "worker", "send_blocked", {"outbox_id": row["id"], "reasons": reasons})
                continue
            if not ctx.external_allowed():                           # kill switch flipped after the claim
                ctx.db.outbox_requeue(row["id"], "kill switch", now)
                return ctx.partial({"sent": sent})
            try:
                res = env.mailer.send(pl["to"], pl["subject"], pl["body"], row["idem_key"])
            except mailmod.MailError as exc:
                if exc.kind == "rejected":
                    ctx.db.outbox_fail(row["id"], "Gmail rejected it", env.clock())
                    env.store.update_business(bid, status="rejected", hold_reason="Gmail rejected the address", now=env.clock())
                    env.store.audit(cid, "worker", "send_rejected", {"business_id": bid})
                    continue
                if exc.kind in ("auth", "restricted"):
                    ctx.db.outbox_requeue(row["id"], exc.kind, env.clock())
                    _gmail_trouble(ctx, env, cid, exc, now)
                ctx.db.outbox_mark_uncertain(row["id"], f"{exc.kind}: outcome unknown", env.clock())      # may have gone out
                _emit(ctx, "offer_sent", cid, biz, status="uncertain", evidence={"outbox_id": row["id"]}, source_suffix=":u")
                return {"sent": sent, "uncertain": 1}                # stop for this run: something is wrong with the connection
            ctx.db.outbox_sent(row["id"], res["message_id"], res.get("thread_id", ""), env.clock())
            sent += 1
            env.store.update_business(bid, stage="sent", data={"offer": {**biz["data"].get("offer", {}), "state": "sent",
                                      "message_id": res["message_id"], "thread_id": res.get("thread_id", ""), "sent_at": env.clock()}}, now=env.clock())
            _emit(ctx, "offer_sent", cid, biz, status="sent", ts=env.clock(),
                  evidence={"message_id": res["message_id"], "thread_id": res.get("thread_id", ""), "from": res.get("from")})
            env.store.audit(cid, "worker", "offer_sent", {"business_id": bid, "message_id": res["message_id"]})
            if env.pace and not ctx.should_stop():
                env.sleep(env.pace)
    return {"sent": sent}


# ── replies ──────────────────────────────────────────────────────────────────
def _take_down(env: Env, biz: dict) -> None:
    slug = (biz["data"] or {}).get("slug")
    if slug and env.host.public:
        try:
            env.host.remove(slug)
        except Exception:
            pass                                                    # the 30-day expiry still removes it


def _handle_reply(ctx, env: Env, c: dict, p, biz: dict, m: dict, thread: str, me: str) -> int:
    cid = c["id"]
    sender = mailmod.address_of(m["from"])
    if "SENT" in (m.get("labels") or []) or sender == me:
        return 0
    res = replies.classify(m["text"], from_addr=m["from"], subject=m["subject"], headers=m.get("headers"))
    if not env.store.add_reply(biz["id"], cid, m["id"], thread, m["ts"], res["classification"], res["confidence"],
                               replies.snippet(m["text"]), res["needs_review"]):
        return 0
    cl = res["classification"]
    status = cl if not res["needs_review"] or cl in ("opted_out", "bounced", "complaint") else "unclear"
    ctx.events.record("reply_received", source="gmail", source_id=f"reply:{m['id']}", ts=m["ts"], task_id=str(biz["id"]),
                      campaign_id=cid, status=status, title=messages.clean(biz["name"], 60), private=True,
                      detail={"snippet": replies.snippet(m["text"], 140), "confidence": res["confidence"],
                              "guess": cl, "evidence": res["evidence"]},
                      evidence={"message_id": m["id"], "thread_id": thread})
    if cl in ("opted_out", "complaint", "bounced", "declined"):
        keys = [biz["email"]] if cl in ("bounced", "declined") else suppression_keys(biz["email"], biz["website"])
        for k in keys:
            env.store.suppress(k, cl, "reply")
        env.store.update_business(biz["id"], status="suppressed", hold_reason=cl, now=env.clock())
        env.store.audit(cid, "worker", f"suppressed:{cl}", {"business_id": biz["id"]})
        _take_down(env, biz)
    if cl == "complaint":
        _stop(ctx, env, cid, "a recipient complained")
    if cl == "bounced":
        bounces = sum(1 for r in env.store.replies(cid) if r["classification"] == "bounced")
        if bounces >= p.max_bounces:
            _stop(ctx, env, cid, f"{bounces} emails bounced")
    return 1


def h_replies(ctx, job, env: Env) -> dict:
    new = 0
    for c in env.store.campaigns():
        if c["status"] not in ("active", "stopped"):
            continue
        cid = c["id"]
        _, p = _policy(env, cid)
        try:
            me = (env.mailer.check() or "").lower()
        except mailmod.MailError as exc:
            ctx.events.sync_failed("gmail", f"{exc.kind}")
            if exc.kind in ("auth", "restricted"):
                if c["status"] == "active":
                    _stop(ctx, env, cid, "Gmail sign-in problem")
                raise NeedsSetup("Reconnect the spam Gmail account in Jarvis so replies can be read")
            raise Deferred(env.clock() + 900, f"Gmail not reachable right now ({exc.kind})")
        sent = [b for b in env.store.businesses(cid, stage="sent")]
        for biz in sent:
            thread = biz["data"].get("offer", {}).get("thread_id")
            if not thread or ctx.should_stop():
                continue
            try:
                msgs = env.mailer.thread_messages(thread)
            except mailmod.MailError as exc:
                ctx.events.sync_failed("gmail", exc.kind)
                continue
            for m in msgs:
                if m["id"] == biz["data"].get("offer", {}).get("message_id"):
                    continue
                new += _handle_reply(ctx, env, c, p, biz, m, thread, me)
        # a new message (not a reply in our thread): an opt-out typed fresh, or a bounce Gmail filed separately
        try:
            inbox = env.mailer.inbox_since(3) if hasattr(env.mailer, "inbox_since") else []
        except mailmod.MailError as exc:
            ctx.events.sync_failed("gmail", exc.kind)
            inbox = []
        by_addr = {b["email"].lower(): b for b in sent if b["email"]}
        for m in inbox:
            addr = mailmod.address_of(m["from"])
            biz = by_addr.get(addr)
            if biz is None and replies.BOUNCE_FROM.search(m["from"] or ""):
                low = (m["text"] or "").lower()
                biz = next((b for a, b in by_addr.items() if a in low), None)
            if biz is not None:
                new += _handle_reply(ctx, env, c, p, biz, m, biz["data"].get("offer", {}).get("thread_id", ""), me)
    ctx.events.sync_ok("gmail", stale_after=3600, at=env.clock())
    return {"new_replies": new}


# ── housekeeping ─────────────────────────────────────────────────────────────
def h_expire(ctx, job, env: Env) -> dict:
    gone = []
    ttl = min([pol.from_dict(c["policy"]).preview_ttl_days for c in env.store.campaigns()] or [30])
    if env.host.public and ctx.external_allowed():
        gone = env.host.expire(env.clock(), ttl)
    return {"removed": gone}


def handlers(env: Optional[Env] = None) -> dict:
    cache: dict = {"env": env}

    def e() -> Env:
        if cache["env"] is None:
            cache["env"] = default_env()
        return cache["env"]

    def wrap(fn):
        return lambda ctx, job: fn(ctx, job, e())
    return {"campaign_discover": wrap(h_discover), "campaign_audit": wrap(h_audit), "generate_site": wrap(h_generate),
            "campaign_publish": wrap(h_publish), "campaign_offer": wrap(h_offer), "campaign_send": wrap(h_send),
            "campaign_replies": wrap(h_replies), "campaign_expire": wrap(h_expire)}


def seed_schedules(db, env: Optional[Env] = None) -> None:
    """Recurring work. Sending is time-sensitive: a slot more than 20 minutes late is dropped, not replayed."""
    db.schedule("campaign_send", "campaign_send", 600, catchup="skip_expired", max_lateness_s=1200)
    db.schedule("campaign_replies", "campaign_replies", 600, catchup="run_once")
    db.schedule("campaign_expire", "campaign_expire", 86400, catchup="run_once")
    try:
        store = (env or default_env()).store
        for c in store.campaigns():
            schedule_campaign(db, c["id"])
    except Exception:
        pass


def schedule_campaign(db, cid: str) -> None:
    """Daily discovery for one campaign. Safe to call again (the schedule is keyed by name)."""
    db.schedule(f"discover:{cid}", "campaign_discover", 86400, payload={"campaign_id": cid}, campaign_id=cid,
                catchup="skip_expired", max_lateness_s=6 * 3600)
