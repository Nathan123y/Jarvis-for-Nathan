"""Finding local businesses from open data (OpenStreetMap), and not pitching one twice.

OpenStreetMap is free, open (ODbL) and meant to be queried; the Overpass API asks clients to be polite:
one request at a time, a descriptive User-Agent, and a pause between requests. No login, CAPTCHA or
access control is ever bypassed. A listing is a *lead*, not proof: later steps verify the business,
look for its real website, and require a published contact before anything is built or sent.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional

from worker.campaign import net
from worker.campaign.policy import AREAS

USER_AGENT = "JarvisLocalSiteConcepts/1.0 (personal project; polite single-request client)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PAUSE_SECONDS = 3.0

CATEGORY_TAGS = {
    "plumber": ("craft", "plumber"), "electrician": ("craft", "electrician"), "hvac": ("craft", "hvac"),
    "roofer": ("craft", "roofer"), "painter": ("craft", "painter"), "carpenter": ("craft", "carpenter"),
    "gardener": ("craft", "gardener"), "locksmith": ("craft", "locksmith"), "tailor": ("craft", "tailor"),
    "photographer": ("craft", "photographer"), "hairdresser": ("shop", "hairdresser"),
    "barber": ("shop", "hairdresser"), "beauty": ("shop", "beauty"), "car_repair": ("shop", "car_repair"),
    "cleaning": ("shop", "laundry"), "dog_grooming": ("shop", "pet_grooming"),
}

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "me.com",
             "live.com", "msn.com", "proton.me", "protonmail.com", "comcast.net", "att.net", "sbcglobal.net",
             "googlemail.com", "ymail.com", "mac.com"}
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "yelp.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
                "nextdoor.com", "angi.com", "thumbtack.com", "yellowpages.com", "google.com", "goo.gl", "maps.app.goo.gl")


# ── normalising ──────────────────────────────────────────────────────────────
def digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d


def norm_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(inc|llc|co|company|corp|the|and|services?|solutions?)\b", " ", s)
    return " ".join(s.split())


def host_of(url: str) -> str:
    try:
        h = urllib.parse.urlsplit(url if "//" in url else "//" + url).hostname or ""
    except ValueError:
        return ""
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def is_social_or_directory(url: str) -> bool:
    h = host_of(url)
    return any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS)


def clean_email(value: str) -> str:
    v = (value or "").strip().lower().split(";")[0].strip()
    return v if re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", v) else ""


def keys_for(b: dict) -> list[str]:
    """Every identity a business can be recognised by: phone, name+city, website domain, email."""
    out = []
    if digits(b.get("phone", "")) and len(digits(b["phone"])) == 10:
        out.append("p:" + digits(b["phone"]))
    if norm_name(b.get("name", "")):
        out.append("n:" + norm_name(b["name"]) + "|" + (b.get("city") or "").strip().lower())
    if b.get("website") and not is_social_or_directory(b["website"]):
        out.append("d:" + host_of(b["website"]))
    if clean_email(b.get("email", "")):
        out.append("e:" + clean_email(b["email"]))
    return out


class Deduper:
    """Remembers every identity ever seen (all campaigns), so a business is never found or pitched twice."""

    def __init__(self, seen: Iterable[str] = ()):
        self.seen = set(seen)

    @classmethod
    def from_store(cls, store) -> "Deduper":
        seen = set()
        for c in store.campaigns():
            for b in store.businesses(c["id"]):
                seen.update(b["data"].get("keys") or [b["dedupe_key"]])
        return cls(seen)

    def is_new(self, b: dict) -> bool:
        return not any(k in self.seen for k in keys_for(b))

    def add(self, b: dict) -> None:
        self.seen.update(keys_for(b))


# ── providers ────────────────────────────────────────────────────────────────
def overpass_query(area: str, categories: Iterable[str], limit: int = 300) -> str:
    s, w, n, e = AREAS[area]
    seen, parts = set(), []
    for c in categories:
        tag = CATEGORY_TAGS.get(c)
        if tag and tag not in seen:
            seen.add(tag)
            parts.append(f'  nwr["{tag[0]}"="{tag[1]}"]["name"]({s},{w},{n},{e});')
    return "[out:json][timeout:60];\n(\n" + "\n".join(parts) + f"\n);\nout center tags {int(limit)};"


def _addr(tags: dict) -> tuple[str, str]:
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    city = tags.get("addr:city", "")
    full = ", ".join(x for x in (street, city, tags.get("addr:state"), tags.get("addr:postcode")) if x)
    return full, city


def parse_overpass(payload: dict, area: str, retrieved_at: float) -> list[dict]:
    out = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or any(k.startswith(("disused:", "abandoned:", "was:")) for k in tags):
            continue
        address, city = _addr(tags)
        category = next((c for c, (k, v) in CATEGORY_TAGS.items() if tags.get(k) == v), "")
        site = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
        osm = f"{el.get('type', 'node')}/{el.get('id')}"
        out.append({
            "name": name, "category": category, "address": address, "city": city,
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "email": clean_email(tags.get("email") or tags.get("contact:email") or ""),
            "website": site, "source": "openstreetmap", "source_id": osm,
            "data": {"osm_url": f"https://www.openstreetmap.org/{osm}", "retrieved_at": retrieved_at,
                     "area": area, "hours": tags.get("opening_hours", ""), "osm_edited": el.get("timestamp", ""),
                     "social": [v for k, v in tags.items() if k.startswith("contact:") and v
                                and k.split(":")[1] in ("facebook", "instagram", "yelp")]},
        })
    return out


class OverpassProvider:
    def __init__(self, fetch: Optional[Callable[[str, bytes], dict]] = None, sleep=time.sleep,
                 clock: Callable[[], float] = time.time):
        self._fetch, self._sleep, self._clock = fetch or self._http, sleep, clock
        self._last = 0.0

    @staticmethod
    def _http(url: str, body: bytes) -> dict:
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT,
                                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=90, context=net.ssl_context()) as r:
            return json.loads(r.read(8_000_000).decode("utf-8", "replace"))

    def search(self, area: str, categories: list[str], limit: int = 300) -> list[dict]:
        wait = PAUSE_SECONDS - (self._clock() - self._last)
        if self._last and wait > 0:
            self._sleep(wait)
        body = urllib.parse.urlencode({"data": overpass_query(area, categories, limit)}).encode()
        payload = self._fetch(OVERPASS_URL, body)
        self._last = self._clock()
        return parse_overpass(payload, area, self._clock())


class FixtureProvider:
    """Fixed businesses for tests and the dry run: nothing is fetched."""

    def __init__(self, businesses: list[dict]):
        self.businesses = businesses

    def search(self, area: str, categories: list[str], limit: int = 300) -> list[dict]:
        return [dict(b) for b in self.businesses][:limit]


def select_candidates(found: list[dict], deduper: Deduper, limit: int) -> list[dict]:
    """New businesses only, best leads first: a listed email (so there's a published contact) and no
    listed website come first; ones with a site are kept too because the site may be poor."""
    fresh = []
    for b in found:
        if deduper.is_new(b):
            deduper.add(b)
            fresh.append(b)
    fresh.sort(key=lambda b: (not clean_email(b.get("email", "")), bool(b.get("website")), b["name"].lower()))
    return fresh[:limit]
