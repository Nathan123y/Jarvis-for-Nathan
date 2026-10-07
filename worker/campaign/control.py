"""Controls for the website campaign, shared by the `worker campaign` command and the Jarvis plugin.

Nothing here sends anything. `enable` is the one-time authorization: the campaign is checked for
everything it needs (sender name, postal address, public preview hosting, a working Gmail sign-in)
and refuses to start otherwise.
"""
from __future__ import annotations

import time
from typing import Optional

from worker.campaign import pipeline, policy as pol
from worker.campaign.store import CampaignStore

DEFAULT_ID = "bay-area-websites"
DEFAULT_NAME = "Local business websites, San Jose and Bay Area"


def ensure(store: CampaignStore, cid: str = DEFAULT_ID, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    if store.campaign(cid) is None:
        store.save_campaign(cid, DEFAULT_NAME, pol.Policy().__dict__.copy(), now)
        store.set_campaign(cid, now, mode="draft", status="active")
        store.audit(cid, "system", "created", {"policy_hash": pol.policy_hash(pol.Policy())}, now)
    return store.campaign(cid)


def readiness(env: "pipeline.Env", *, check_gmail: bool = False) -> list[str]:
    """What is missing before anything can be sent. Empty = ready."""
    missing = []
    ident = env.identity()
    if not ident.get("sender_name"):
        missing.append("your sender name (Plugin Settings, Product promotion)")
    if len((ident.get("postal_address") or "").strip()) < 10:
        missing.append("your postal address (Plugin Settings, Product promotion): commercial email must carry one")
    if not getattr(env.host, "public", False):
        missing.append("public preview hosting (Plugin Settings, Website campaign: repository URL and page address)")
    if check_gmail:
        try:
            env.mailer.check()
        except Exception as exc:
            missing.append(f"the spam Gmail account signed in to Jarvis ({getattr(exc, 'detail', '') or type(exc).__name__})")
    return missing


def samples(store: CampaignStore, cid: str, limit: int = 3) -> list[dict]:
    out = []
    for b in store.businesses(cid):
        offer = b["data"].get("offer")
        if offer and b["status"] == "ok":
            out.append({"name": b["name"], "preview": b["data"].get("preview_url", ""), "subject": offer["subject"], "body": offer["body"]})
        if len(out) >= limit:
            break
    return out


def review(env: "pipeline.Env", cid: str = DEFAULT_ID) -> str:
    c = ensure(env.store, cid)
    p = pol.from_dict(c["policy"])
    schedule = "discovery once a day, sending checks every 10 minutes, replies read every 10 minutes (only while the Mac is awake and you are logged in)"
    text = pol.review_text(p, identity=env.identity(), hosting={"summary": env.host.summary()}, samples=samples(env.store, cid), schedule=schedule)
    missing = readiness(env)
    if missing:
        text += "\nBEFORE IT CAN START: " + "; ".join(missing) + "."
    return text


def enable(env: "pipeline.Env", db, cid: str = DEFAULT_ID, *, by: str, check_gmail: bool = True) -> str:
    """The one-time authorization. After this no per-email approval is needed, within the policy's limits."""
    c = ensure(env.store, cid)
    missing = readiness(env, check_gmail=check_gmail)
    if missing:
        return "Not enabled. Still needed: " + "; ".join(missing) + "."
    now = env.clock()
    info = pol.authorize(env.store, cid, by=by, now=now)
    pipeline.schedule_campaign(db, cid)
    db.enqueue("campaign_discover", {"campaign_id": cid}, campaign_id=cid, unique_key=f"discover:{cid}:{int(now)}")
    for b in env.store.businesses(cid, stage="preview", status="ok"):          # drafts made before authorization get queued
        db.enqueue("campaign_offer", {"business_id": b["id"]}, campaign_id=cid, unique_key=f"offer:{b['id']}:{int(now)}", max_attempts=2)
    until = time.strftime("%b %d", time.localtime(info["until"]))
    p = pol.from_dict(env.store.campaign(cid)["policy"])
    return (f"Enabled until {until}. Jarvis will find, build, check and pitch businesses on its own, {pol.limit_text(p)} "
            f"at {pol.price_text(p)}, only Mon-Fri {p.send_from_hour}-{p.send_to_hour} Pacific, only while the background worker is running. "
            "It stops by itself on Gmail errors, a complaint or bounces, and you can say 'stop the campaign' any time.")


def stop(env: "pipeline.Env", cid: str = DEFAULT_ID, *, by: str, reason: str = "stopped by you") -> str:
    ensure(env.store, cid)
    pol.revoke(env.store, cid, by=by, reason=reason, now=env.clock())
    return "Campaign stopped and authorization revoked. Nothing more will be sent; messages already queued are held."


def pause(env: "pipeline.Env", cid: str = DEFAULT_ID, *, by: str) -> str:
    c = ensure(env.store, cid)
    if c["status"] != "active":
        return f"The campaign is {c['status']}, not running."
    env.store.set_campaign(cid, env.clock(), status="paused")
    env.store.audit(cid, by, "paused", {}, env.clock())
    return "Paused. Nothing is found, built or sent until you resume it."


def resume(env: "pipeline.Env", cid: str = DEFAULT_ID, *, by: str) -> str:
    c = ensure(env.store, cid)
    if c["status"] != "paused":
        return f"The campaign is {c['status']}" + (": it needs a fresh authorization." if c["status"] == "stopped" else ", not paused.")
    env.store.set_campaign(cid, env.clock(), status="active")
    env.store.audit(cid, by, "resumed", {}, env.clock())
    return "Resumed."


def set_mode(env: "pipeline.Env", mode: str, cid: str = DEFAULT_ID, *, by: str) -> str:
    if mode not in ("research", "draft"):
        return "Mode can be set to research or draft here. Autonomous only comes from the one-time authorization."
    ensure(env.store, cid)
    env.store.set_campaign(cid, env.clock(), mode=mode)
    env.store.audit(cid, by, "mode", {"mode": mode}, env.clock())
    return {"research": "Research only: businesses are found and checked, nothing is built or written.",
            "draft": "Draft only: sites and messages are prepared, nothing is queued or sent."}[mode]


def status(env: "pipeline.Env", cid: str = DEFAULT_ID, db=None) -> str:
    c = ensure(env.store, cid)
    auth = pol.authorization(env.store, cid, env.clock())
    p = pol.from_dict(c["policy"])
    stages = env.store.count_by_stage(cid)
    lines = [f"Campaign: {c['name']}", f"Status: {c['status']}" + (f" ({c['stop_reason']})" if c["stop_reason"] else "") + f", mode {c['mode']}",
             f"Authorization: {auth['state']}" + (f" ({auth['reason']})" if auth["reason"] else ""),
             f"Offer: {pol.price_text(p)}; daily limit {p.daily_cap or 'none'}; batch {p.batch_size}",
             "Businesses by stage: " + (", ".join(f"{k} {v}" for k, v in stages.items()) or "none yet")]
    held = [b for b in env.store.businesses(cid) if b["status"] in ("held", "backlog")][:5]
    for b in held:
        lines.append(f"  held: {b['name']}: {b['hold_reason']}")
    missing = readiness(env)
    if missing:
        lines.append("Still needed: " + "; ".join(missing))
    return "\n".join(lines)
