"""The outreach email: short, courteous, honest, and checked before it can be queued.

It says who is writing, labels the site an independent concept, mentions ONE verified thing (a problem
that was actually observed, or that no website could be found after the listed checks), gives the working
preview, states the exact price from the campaign policy, and asks whether the owner is interested. It
carries the sender's postal address, says it is a commercial message, and tells the owner how to stop
(reply STOP). It makes no claim about results, rankings, reviews or affiliation.
"""
from __future__ import annotations

import re

from worker.campaign.policy import Policy, price_text

PROBLEM_SENTENCES = {
    "unreachable": "your website didn't load when I tried it twice",
    "http_error": "your website's home page shows an error page",
    "placeholder_page": "your website is still a placeholder page",
    "not_mobile_friendly": "your website isn't set up for phones, so it shows up shrunken on a mobile screen",
    "no_contact_details": "your website's home page doesn't show a phone number or email address",
    "broken_links": "some links on your website lead to error pages",
    "uses_flash": "your website relies on Flash, which browsers no longer run",
    "no_https": "your website isn't served securely (no HTTPS), so browsers warn visitors",
    "very_little_content": "your website's home page has almost no text on it",
}
PRIORITY = ["unreachable", "http_error", "placeholder_page", "uses_flash", "not_mobile_friendly",
            "no_contact_details", "broken_links", "no_https", "very_little_content"]


def clean(value: str, limit: int = 80) -> str:
    return " ".join(re.sub(r"[\r\n\t]+", " ", str(value or "")).split())[:limit]


def observation(biz: dict, problems: list[dict], no_website_checks: list[str]) -> tuple[str, str]:
    """(sentence for the email, internal evidence label). Only verified findings are used."""
    name = clean(biz["name"])
    if problems:
        codes = {p["code"]: p for p in problems}
        for c in PRIORITY:
            if c in codes:
                return f"While looking at {name} online I noticed {PROBLEM_SENTENCES[c]}.", c
    checks = (", ".join(no_website_checks[:-1]) + " and " + no_website_checks[-1]) if len(no_website_checks) > 1 else (no_website_checks[0] if no_website_checks else "online listings")
    return (f"I looked for an official website for {name} (I checked {checks}) and couldn't find one.", "no_website_verified")


def compose(biz: dict, problems: list[dict], no_website_checks: list[str], preview_url: str,
            policy: Policy, identity: dict) -> dict:
    name, sender, address = clean(biz["name"]), clean(identity["sender_name"], 60), clean(identity["postal_address"], 160)
    sentence, basis = observation(biz, problems, no_website_checks)
    price = price_text(policy)
    body = (
        f"Hello,\n\n"
        f"My name is {sender}. {sentence} "
        f"So I put together a quick concept of what a one-page site for {name} could look like:\n\n"
        f"{preview_url}\n\n"
        f"It's an independent concept. It isn't affiliated with you, it only uses public details I found about the business, "
        f"and it has sample placeholders where your real services would go.\n\n"
        f"If you like it, I can build the finished one-page site for {price}, one time, with one round of revisions "
        f"(a domain and hosting would be separate). Would you be interested?\n\n"
        f"If not, just reply STOP and I won't contact you again.\n\n"
        f"{sender}\n{address}\n"
        f"This is a commercial message from {sender}."
    )
    return {"subject": f"A website concept for {name}", "body": body, "basis": basis}


def validate(msg: dict, biz: dict, policy: Policy, identity: dict, preview_url: str) -> list[str]:
    """Reasons this message must not be sent (empty = fine). Run again at send time."""
    body, subj = msg["body"], msg["subject"]
    bad = []
    if not clean(identity.get("sender_name")):
        bad.append("sender name missing")
    if len(clean(identity.get("postal_address"))) < 10:
        bad.append("valid postal address missing (required for commercial email)")
    elif clean(identity["postal_address"]) not in body:
        bad.append("postal address not in the message")
    if "commercial message" not in body.lower():
        bad.append("not identified as a commercial message")
    if not re.search(r"reply\s+stop", body, re.I):
        bad.append("no working opt-out")
    if not preview_url.startswith("https://") or preview_url not in body:
        bad.append("working https preview link missing")
    if price_text(policy) not in body or body.count("$") != 1:
        bad.append("price in the message doesn't match the campaign policy")
    if "independent concept" not in body.lower():
        bad.append("concept label missing")
    if re.match(r"\s*(re|fwd?):", subj, re.I):
        bad.append("subject looks like a reply or forward")
    if len(body.split()) > 170:
        bad.append("too long")
    if re.search(r"(\bguarantee\w*|\brank\w*|#\s?1\b|\btop[- ]rated|\baward\w*|\blimited time|\bact now|\bfree money|\burgent\w*)", body, re.I):
        bad.append("contains a claim or pressure phrase")
    stray = [u for u in re.findall(r"(?:https?://|www\.)\S+", subj + "\n" + body, re.I) if u.rstrip(".,)") != preview_url.rstrip(".,)")]
    if stray:
        bad.append("contains a link other than the preview")
    nm = str(biz.get("name") or "")
    if re.search(r"@|https?:|www\.|\.(com|net|org|io|co)\b", nm, re.I) or len(re.findall(r"\d", nm)) > 6:
        bad.append("business name looks like contact details or a link (listing text can be edited by anyone)")
    if not msg.get("basis"):
        bad.append("no verified observation")
    to = biz.get("email") or ""
    if not to:
        bad.append("no published contact email")
    return bad
