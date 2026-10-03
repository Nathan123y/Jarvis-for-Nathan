"""On-demand, review-first traffic drafts for an owner-supplied Etsy shop.

Nothing runs in the background and nothing posts automatically.  The plugin
turns one verified listing URL into trackable social drafts and a practical
listing audit, while avoiding promises about rankings, traffic, or revenue.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from memory.config_manager import get_plugin_config


PLUGIN = {
    "name": "etsy_growth",
    "behavior": "NON_BLOCKING",
    "description": (
        "Etsy-only helper. Prepare ethical, review-first traffic ideas for the user's Etsy shop. "
        "Actions: audit checks a listing's merchandising basics; listing creates a "
        "complete reviewable listing pack; campaign creates three trackable social "
        "post drafts; reply drafts a response to an inbound buyer question; status "
        "checks setup. Requires an "
        "owner-supplied public Etsy shop or listing URL in Plugin Settings. Never "
        "claim guaranteed leads, rankings, sales, or revenue, and never post or "
        "spend money automatically. Never use this tool for Gumroad products or outreach."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status, audit, listing, campaign, or reply"},
            "listing_url": {"type": "STRING", "description": "Optional public etsy.com listing URL; otherwise the configured shop URL is used"},
            "product_name": {"type": "STRING", "description": "Exact product name for campaign copy"},
            "audience": {"type": "STRING", "description": "Specific intended buyer, such as private soccer coaches"},
            "benefit": {"type": "STRING", "description": "One accurate, supportable product benefit"},
            "contents": {"type": "STRING", "description": "What the buyer receives; required for listing"},
            "requirements": {"type": "STRING", "description": "Software, materials, or buyer requirements; required for listing"},
            "keywords": {"type": "STRING", "description": "Comma-separated accurate search phrases; up to 13"},
            "buyer_message": {"type": "STRING", "description": "An inbound buyer question to answer; required for reply"},
            "answer": {"type": "STRING", "description": "Accurate answer or offer terms supplied by the owner; required for reply"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "etsy_growth",
    "title": "Etsy shop — listings and traffic",
    "fields": [
        {"key": "shop_url", "type": "text", "label": "Public Etsy shop URL",
         "placeholder": "https://www.etsy.com/shop/YourShop"},
    ],
}


def _etsy_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold()
        valid = (parsed.scheme == "https" and host in {"etsy.com", "www.etsy.com"}
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443)
                 and (parsed.path.startswith("/shop/") or parsed.path.startswith("/listing/")))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Add a public HTTPS Etsy shop or listing URL, such as "
            "https://www.etsy.com/shop/YourShop."
        )
    return urlunsplit(("https", "www.etsy.com", parsed.path.rstrip("/"), parsed.query, ""))


def _destination(parameters: dict) -> str:
    configured = get_plugin_config("etsy_growth") or {}
    return _etsy_url(parameters.get("listing_url") or configured.get("shop_url"))


def _tracked(url: str, content: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(utm_source="jarvis", utm_medium="social",
                 utm_campaign="etsy_shop_growth", utm_content=content)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _clean(value: object, label: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"Give the {label}; Jarvis will not invent product claims.")
    return text[:limit]


def _listing_pack(parameters: dict, url: str) -> str:
    name = _clean(parameters.get("product_name"), "exact product name")
    audience = _clean(parameters.get("audience"), "intended buyer")
    benefit = _clean(parameters.get("benefit"), "accurate product benefit", 240)
    contents = _clean(parameters.get("contents"), "exact package contents", 700)
    requirements = _clean(parameters.get("requirements"), "buyer requirements", 400)
    raw_keywords = str(parameters.get("keywords") or "")
    tags = []
    for keyword in raw_keywords.split(","):
        tag = " ".join(keyword.split()).casefold()[:20]
        if tag and tag not in tags:
            tags.append(tag)
    if not tags:
        raise ValueError("Give accurate comma-separated search phrases; Jarvis will not invent Etsy tags.")
    tags = tags[:13]
    title = f"{name} for {audience}"[:140]
    return (
        f"ETSY LISTING PACK — REVIEW BEFORE CREATING\nDestination: {url}\n\n"
        f"TITLE\n{title}\n\nDESCRIPTION\n{name} is for {audience}.\n\n"
        f"What it helps with\n{benefit}\n\nWhat you receive\n{contents}\n\n"
        f"Requirements and limitations\n{requirements}\n\n"
        f"TAGS ({len(tags)}/13)\n" + " | ".join(tags) +
        "\n\nIMAGE PLAN\n1. Clear product cover\n2. Everything included\n"
        "3. Main workflow or result\n4. Requirements and compatibility\n"
        "5. Accurate example or close-up\n\n"
        "This is a local draft. Jarvis did not sign in, create a listing, set a price, "
        "charge a listing fee, or publish anything. Confirm ownership, Etsy eligibility, "
        "pricing, files, images, tax, and disclosure details in Etsy before publishing."
    )


def _buyer_reply(parameters: dict) -> str:
    message = _clean(parameters.get("buyer_message"), "inbound buyer message", 800)
    answer = _clean(parameters.get("answer"), "owner-verified answer or offer terms", 800)
    return (
        "INBOUND BUYER REPLY — REVIEW BEFORE SENDING\n\n"
        f"Buyer asked:\n{message}\n\nDraft reply:\nHi, thanks for reaching out. {answer}\n\n"
        "Please let me know if you have another question before ordering.\n\n"
        "Nothing was sent. Use this only to answer a person who contacted your shop; "
        "do not use Etsy Messages for unsolicited advertising, and keep checkout on Etsy."
    )


def _show(player, title: str, body: str) -> None:
    if player and callable(getattr(player, "show_content", None)):
        player.show_content(title, body[:3900])


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "").strip().casefold()
    if action == "status":
        try:
            url = _destination(parameters)
        except ValueError as exc:
            return f"Etsy traffic setup is incomplete. {exc}"
        return (f"Etsy destination ready: {url}\n"
                "Campaigns are created only when requested and are never posted automatically. "
                "Traffic and revenue are not guaranteed.")

    url = _destination(parameters)
    if action == "audit":
        report = (
            f"ETSY LISTING AUDIT\nDestination: {url}\n\n"
            "Review these conversion basics in Etsy:\n"
            "1. Put the exact product and intended buyer in the opening title words.\n"
            "2. Lead with a clear, readable first image; show what the buyer receives.\n"
            "3. Use all relevant Etsy tags without repeating vague phrases.\n"
            "4. State file types, requirements, limits, and what is not included.\n"
            "5. Preview the product in use and add an accurate FAQ.\n"
            "6. Compare visits, favourites, and orders in Etsy Stats before changing one variable at a time.\n\n"
            "This is a checklist, not a claim that Etsy search placement or sales will improve."
        )
        _show(player, "ETSY LISTING AUDIT", report)
        return report

    if action == "listing":
        report = _listing_pack(parameters, url)
        _show(player, "ETSY LISTING PACK", report)
        return report

    if action == "campaign":
        name = _clean(parameters.get("product_name"), "exact product name")
        audience = _clean(parameters.get("audience"), "intended buyer")
        benefit = _clean(parameters.get("benefit"), "accurate product benefit", 240)
        drafts = [
            ("problem", f"For {audience}: {benefit} Explore {name}:"),
            ("preview", f"A closer look at {name}. Built for {audience}, with a clear goal: {benefit}"),
            ("question", f"What is the hardest part of this workflow for {audience}? {name} is designed to help with one practical need: {benefit}"),
        ]
        body = "\n\n".join(
            f"DRAFT {index} — {key.upper()}\n{text}\n{_tracked(url, key)}"
            for index, (key, text) in enumerate(drafts, 1)
        )
        body += ("\n\nReview every claim and the destination before posting. Use only accounts "
                 "and communities where promotion is allowed. Nothing was posted or purchased.")
        _show(player, "ETSY TRAFFIC DRAFTS", body)
        return body

    if action == "reply":
        reply = _buyer_reply(parameters)
        _show(player, "ETSY BUYER REPLY", reply)
        return reply

    return "Choose status, audit, listing, campaign, or reply."
