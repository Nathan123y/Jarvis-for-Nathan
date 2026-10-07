"""The campaign policy and the authorization that lets it run without per-email approval.

Ordinary one-off messages in Jarvis still ask for confirmation. A campaign is different: you review
one policy (area, sender, offer, hosting, schedule, limits, budget, stop conditions), authorize it
once, and from then on actions that match it run on their own. The authorization is tied to a hash of
the policy's material fields: change the price, area, sender or limits and it stops working until you
authorize the new version. It has an expiry, can be revoked at any time, and every step is written to
an audit trail. Anything outside the policy stops for review.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from core.away import tz
from worker.campaign.store import CampaignStore

MODES = ("research", "draft", "autonomous")

# area name -> (south, west, north, east): the open-data search boxes
AREAS = {
    "san-jose": (37.12, -122.05, 37.47, -121.58),
    "santa-clara": (37.30, -122.05, 37.42, -121.92),
    "sunnyvale-cupertino": (37.28, -122.10, 37.43, -121.96),
    "mountain-view-palo-alto": (37.35, -122.20, 37.47, -122.04),
    "fremont-milpitas": (37.40, -122.10, 37.58, -121.85),
    "oakland-berkeley": (37.75, -122.35, 37.92, -122.15),
    "san-francisco": (37.70, -122.52, 37.83, -122.35),
}

# small local service businesses that are usually findable but often have no real site
DEFAULT_CATEGORIES = ("plumber", "electrician", "hvac", "roofer", "painter", "carpenter", "gardener",
                      "hairdresser", "barber", "beauty", "car_repair", "cleaning", "locksmith", "tailor",
                      "photographer", "tutor", "dog_grooming")

SCOPE_TEXT = ("A custom one-page website: your business details and approved copy, a mobile-friendly "
              "layout, a services section, your contact or existing booking links, and one round of "
              "revisions. Domain registration, hosting and ongoing maintenance are not included and "
              "would be quoted separately.")


@dataclass
class Policy:
    area: str = "san-jose"
    nearby_areas: list = field(default_factory=lambda: ["santa-clara", "sunnyvale-cupertino"])
    categories: list = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    batch_size: int = 20                       # qualified businesses per batch
    daily_cap: int = 20                        # outreach emails per day, across all campaigns
    price_cents: int = 24900                   # one-page site, once (matches the published Soccer Coach Website price)
    scope: str = SCOPE_TEXT
    sender_account: str = "spam"               # the Gmail account Jarvis labels "spam"; never rotated
    send_days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])      # Monday..Friday
    send_from_hour: int = 9
    send_to_hour: int = 17                     # Pacific time
    followups: bool = False
    max_bounces: int = 3                       # stop the campaign after this many bounces
    budget_cents: int = 0                      # new spending authorized: nothing
    preview_ttl_days: int = 30
    require_visual_check: bool = True          # hold a site unless the mobile/desktop browser check passed
    authorization_days: int = 30


MATERIAL = ("area", "nearby_areas", "categories", "batch_size", "daily_cap", "price_cents", "scope",
            "sender_account", "send_days", "send_from_hour", "send_to_hour", "followups", "max_bounces",
            "budget_cents", "preview_ttl_days", "require_visual_check")


def from_dict(d: Optional[dict]) -> Policy:
    base = asdict(Policy())
    base.update({k: v for k, v in (d or {}).items() if k in base})
    return Policy(**base)


def policy_hash(p: Policy) -> str:
    material = {k: getattr(p, k) for k in MATERIAL}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def validate(p: Policy) -> list[str]:
    """Reasons the policy may not run. Limits are hard ceilings: a campaign can't exceed them."""
    bad = []
    for a in [p.area, *p.nearby_areas]:
        if a not in AREAS:
            bad.append(f"unknown area {a!r}")
    if not 1 <= p.batch_size <= 20:
        bad.append("batch size must be 1 to 20")
    if not 1 <= p.daily_cap <= 20:
        bad.append("daily cap must be 1 to 20")
    if p.budget_cents != 0:
        bad.append("budget must be $0: paid services are not authorized")
    if p.followups:
        bad.append("follow-ups are off until a follow-up policy is defined")
    if not 100 <= p.price_cents <= 100_000:
        bad.append("price looks wrong")
    if p.sender_account != "spam":
        bad.append("sender account must be the spam Gmail account")
    if not 0 <= p.send_from_hour < p.send_to_hour <= 24:
        bad.append("send hours are invalid")
    if p.max_bounces < 1:
        bad.append("max bounces must be at least 1")
    return bad


def price_text(p: Policy) -> str:
    return f"${p.price_cents / 100:,.0f}" if p.price_cents % 100 == 0 else f"${p.price_cents / 100:,.2f}"


# ── authorization ────────────────────────────────────────────────────────────
def authorize(store: CampaignStore, cid: str, *, by: str, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    c = store.campaign(cid)
    if c is None:
        raise KeyError(cid)
    p = from_dict(c["policy"])
    problems = validate(p)
    if problems:
        raise ValueError("; ".join(problems))
    until = now + p.authorization_days * 86400
    store.set_campaign(cid, now, authorized_at=now, authorized_hash=policy_hash(p), authorized_until=until,
                       authorized_by=by, revoked_at=None, status="active", stop_reason="", mode="autonomous")
    store.audit(cid, by, "authorized", {"hash": policy_hash(p), "until": until, "price_cents": p.price_cents,
                                        "daily_cap": p.daily_cap}, now)
    return {"until": until, "hash": policy_hash(p)}


def revoke(store: CampaignStore, cid: str, *, by: str, reason: str = "revoked", now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    store.set_campaign(cid, now, revoked_at=now, status="stopped", stop_reason=reason, mode="research")
    store.audit(cid, by, "revoked", {"reason": reason}, now)


def stop(store: CampaignStore, cid: str, reason: str, *, actor: str = "worker", now: Optional[float] = None) -> None:
    """Stop condition hit: no more sends until a person looks and re-authorizes."""
    now = now if now is not None else time.time()
    store.set_campaign(cid, now, status="stopped", stop_reason=reason[:200])
    store.audit(cid, actor, "stopped", {"reason": reason[:200]}, now)


def authorization(store: CampaignStore, cid: str, now: Optional[float] = None) -> dict:
    """{"state": active|none|revoked|expired|changed|stopped, "reason": str}"""
    now = now if now is not None else time.time()
    c = store.campaign(cid)
    if c is None:
        return {"state": "none", "reason": "no such campaign"}
    if c["status"] == "stopped":
        return {"state": "stopped", "reason": c["stop_reason"] or "stopped"}
    if c["revoked_at"]:
        return {"state": "revoked", "reason": "authorization revoked"}
    if not c["authorized_at"]:
        return {"state": "none", "reason": "not authorized yet"}
    if c["authorized_until"] and now > c["authorized_until"]:
        return {"state": "expired", "reason": "authorization expired; review and authorize again"}
    if c["authorized_hash"] != policy_hash(from_dict(c["policy"])):
        return {"state": "changed", "reason": "the policy changed after you authorized it"}
    if c["mode"] != "autonomous":
        return {"state": "none", "reason": f"campaign mode is {c['mode']}"}
    return {"state": "active", "reason": ""}


# ── send gating ──────────────────────────────────────────────────────────────
def local_midnight(now: float) -> float:
    t = datetime.fromtimestamp(now, tz())
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def in_send_window(p: Policy, now: float) -> bool:
    t = datetime.fromtimestamp(now, tz())
    return t.weekday() in p.send_days and p.send_from_hour <= t.hour < p.send_to_hour


def next_window_start(p: Policy, now: float) -> float:
    """The next moment sending is allowed (used to defer a send job without wasting an attempt)."""
    step = 600.0
    t = now
    for _ in range(int(8 * 86400 // step)):
        t += step
        if in_send_window(p, t):
            return t
    return now + 86400


def may_send(store: CampaignStore, cid: str, *, sent_today_all: int, now: Optional[float] = None) -> tuple[bool, str]:
    """(allowed, reason). `sent_today_all` counts sends and possible sends across ALL campaigns today."""
    now = now if now is not None else time.time()
    auth = authorization(store, cid, now)
    if auth["state"] != "active":
        return False, auth["reason"]
    p = from_dict(store.campaign(cid)["policy"])
    if not in_send_window(p, now):
        return False, "outside the allowed sending hours"
    if sent_today_all >= p.daily_cap:
        return False, f"daily cap of {p.daily_cap} reached"
    return True, ""


# ── the review you read before authorizing ───────────────────────────────────
def review_text(p: Policy, *, identity: dict, hosting: dict, samples: list[dict], schedule: str) -> str:
    missing = [k for k in ("sender_name", "postal_address") if not identity.get(k)]
    lines = [
        "WEBSITE CAMPAIGN REVIEW",
        "",
        f"Area: {p.area} plus {', '.join(p.nearby_areas) or 'nothing else'}",
        f"Categories: {', '.join(p.categories)}",
        f"Per batch: up to {p.batch_size} qualified businesses. Daily limit: {p.daily_cap} emails across all campaigns.",
        "",
        f"Sender: the {p.sender_account} Gmail account"
        + (f", as {identity.get('sender_name')}" if identity.get("sender_name") else "")
        + (f", {identity.get('address_email')}" if identity.get("address_email") else ""),
        "Postal address in every email: " + (identity.get("postal_address") or "MISSING (set it in Plugin Settings)"),
        "",
        f"Offer: {price_text(p)} once. {p.scope}",
        "",
        "Preview hosting: " + (hosting.get("summary") or "none"),
        "Schedule: " + schedule,
        f"Sending hours: {p.send_from_hour}:00-{p.send_to_hour}:00 Pacific, "
        + ", ".join("Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in p.send_days),
        f"Budget: ${p.budget_cents / 100:.0f}. No paid APIs, domains, hosting, lead lists or ads.",
        f"Follow-ups: {'on' if p.followups else 'off'}. Replies that propose a call, a negotiation, a custom commitment "
        "or a payment come to you; Jarvis never agrees to those.",
        f"Stops by itself on: Gmail sign-in or sending errors, a spam complaint, {p.max_bounces} bounces, "
        "any opt-out (that business is suppressed immediately), or an authorization that expires "
        f"({p.authorization_days} days) or is revoked.",
        "",
    ]
    if missing:
        lines += ["NOT READY: set " + " and ".join(missing) + " first.", ""]
    for i, s in enumerate(samples, 1):
        lines += [f"SAMPLE {i}: {s.get('name')}", f"  Preview: {s.get('preview') or '(local only)'}",
                  f"  Subject: {s.get('subject')}", *("  " + l for l in str(s.get("body") or "").splitlines()), ""]
    if not samples:
        lines += ["No samples yet: run the dry run first so you can see real sites and messages.", ""]
    return "\n".join(lines)
