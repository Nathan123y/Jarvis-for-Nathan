"""Look at a business's real website, politely, and record observable facts.

Only public pages, fetched the way a browser would, at most a handful per business. robots.txt is
honoured; there is no login, CAPTCHA or access-control bypass; private and internal addresses are
refused (so a hostile page can't point this at the user's own network). Everything that comes back is
untrusted text: it is parsed, never executed, and its instructions mean nothing.

A site problem is only recorded when it can be observed and shown (a URL and what was seen).
"Looks bad to a model" is never a reason.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

from worker.campaign.discover import FREE_MAIL, USER_AGENT as _BASE_UA, clean_email, digits, host_of

USER_AGENT = "JarvisSiteAudit/1.0 (personal project; one polite request at a time)"
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
TIMEOUT = 12


class FetchError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    headers: dict = field(default_factory=dict)
    seconds: float = 0.0


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise FetchError("blocked", "redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _allowed_url(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    return p.scheme in ("http", "https") and bool(p.hostname) and _public_host(p.hostname)


class HttpFetcher:
    """Real network fetcher. Tests use FakeFetcher instead."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_Redirects)
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def allowed_by_robots(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                page = self._get(base + "/robots.txt", text=True)
                if page.status == 200:
                    rp.parse(page.html.splitlines())
                elif page.status in (401, 403) or page.status >= 500:
                    rp.parse(["User-agent: *", "Disallow: /"])        # RFC 9309: unavailable or forbidden means do not crawl
                else:
                    rp.parse([])                                       # 404 and the like: no rules
            except FetchError:
                rp = None                                  # no robots file reachable: nothing forbids us
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> Page:
        if not self.allowed_by_robots(url):
            raise FetchError("robots", "the site's robots.txt asks automated tools not to fetch this")
        page = self._get(url)
        if urllib.parse.urlsplit(page.final_url).netloc != urllib.parse.urlsplit(url).netloc and not self.allowed_by_robots(page.final_url):
            raise FetchError("robots", "the site it redirects to asks automated tools not to fetch it")
        return page

    def _get(self, url: str, text: bool = False) -> Page:
        if not _allowed_url(url):
            raise FetchError("blocked", "not a public http(s) address")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
        started = time.monotonic()
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read(MAX_BYTES)
                ctype = r.headers.get("Content-Type", "")
                html = raw.decode(r.headers.get_content_charset() or "utf-8", "replace") if "html" in ctype or not ctype or (text and ctype.startswith("text/")) else ""
                return Page(url, r.geturl(), r.status, html, dict(r.headers), time.monotonic() - started)
        except urllib.error.HTTPError as exc:
            return Page(url, url, exc.code, "", dict(exc.headers or {}), time.monotonic() - started)
        except FetchError:
            raise
        except socket.timeout as exc:
            raise FetchError("timeout", str(exc)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FetchError("unreachable", str(getattr(exc, "reason", exc))[:150]) from exc


# ── parsing ──────────────────────────────────────────────────────────────────
class _Parse(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.h1, self.viewport, self.links, self.text, self.mailtos, self.tels = 0, False, [], [], [], []
        self.has_flash, self.has_form, self.lang = False, False, ""
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang") or ""
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "meta" and (a.get("name") or "").lower() == "viewport":
            self.viewport = "width" in (a.get("content") or "").lower()
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0])
            elif href.lower().startswith("tel:"):
                self.tels.append(href[4:])
            else:
                self.links.append(href)
        elif tag in ("embed", "object") and ".swf" in ((a.get("src") or a.get("data") or "").lower()):
            self.has_flash = True
        elif tag == "form":
            self.has_form = True
        elif tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PARKED = re.compile(r"(domain (is )?for sale|this domain (may be|is) for sale|buy this domain|coming soon|"
                    r"under construction|website (is )?(coming|under)|account suspended|default web page)", re.I)
ADDRESS_RE = re.compile(r"\b\d{2,6}\s+[A-Za-z0-9.\- ]{3,40}\s+(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|"
                        r"ln|lane|way|ct|court|pl|place|hwy|highway)\b", re.I)


def parse(html: str) -> dict:
    p = _Parse()
    try:
        p.feed(html)
    except Exception:                       # malformed markup must never crash an audit
        pass
    text = " ".join(p.text)
    return {"title": " ".join(p.title.split()), "h1": p.h1, "viewport": p.viewport, "links": p.links,
            "mailtos": p.mailtos, "tels": p.tels, "text": text, "flash": p.has_flash, "lang": p.lang,
            "phones": sorted({digits(m) for m in PHONE_RE.findall(text) if len(digits(m)) == 10}),
            "emails": sorted({e.lower() for e in EMAIL_RE.findall(text)}),
            "has_address": bool(ADDRESS_RE.search(text))}


@dataclass
class Audit:
    url: str
    reachable: bool = False
    skipped: str = ""                       # why it was not audited (robots, blocked)
    problems: list = field(default_factory=list)      # [{code, detail, url}]
    facts: dict = field(default_factory=dict)
    pages: list = field(default_factory=list)          # urls looked at
    emails: list = field(default_factory=list)         # published contact emails found [{email, url}]
    checked_at: float = 0.0


def _problem(audit: Audit, code: str, detail: str, url: str) -> None:
    audit.problems.append({"code": code, "detail": detail, "url": url})


def _same_site(base: str, href: str) -> Optional[str]:
    if href.startswith(("#", "javascript:", "data:")):
        return None
    full = urllib.parse.urljoin(base, href).split("#")[0]
    return full if host_of(full) == host_of(base) and full.startswith("http") else None


def audit_site(url: str, fetcher, *, clock: Callable[[], float] = time.time, link_sample: int = 6,
               pause: Callable[[float], None] = time.sleep) -> Audit:
    """Audit a business's site. Two tries before calling it unreachable (a blip is not a finding)."""
    a = Audit(url=url, checked_at=clock())
    page, last_err = None, None
    for attempt in range(2):
        try:
            page = fetcher.get(url)
            break
        except FetchError as exc:
            last_err = exc
            if exc.kind in ("robots", "blocked"):
                a.skipped = f"{exc.kind}: {exc.detail}"
                return a
            if attempt == 0:
                pause(2.0)
    if page is None:
        a.facts["fetch_error"] = str(last_err)
        _problem(a, "unreachable", f"The site could not be opened twice in a row ({last_err}).", url)
        return a
    a.pages.append(page.final_url)
    if page.status >= 400:
        _problem(a, "http_error", f"The home page answers with HTTP {page.status}.", page.final_url)
        a.facts["status"] = page.status
        return a
    a.reachable = True
    d = parse(page.html)
    a.facts.update({"title": d["title"], "status": page.status, "final_url": page.final_url,
                    "https": page.final_url.startswith("https://"), "seconds": round(page.seconds, 2)})
    if not a.facts["https"]:
        _problem(a, "no_https", "The site is served without HTTPS, so browsers warn visitors.", page.final_url)
    if not d["viewport"]:
        _problem(a, "not_mobile_friendly", "The page has no mobile viewport setting, so phones show a shrunken desktop page.", page.final_url)
    if not (d["tels"] or d["phones"]) and not d["mailtos"] and not d["emails"]:
        _problem(a, "no_contact_details", "No phone number or email address appears on the home page.", page.final_url)
    elif not d["tels"] and d["phones"]:
        a.facts["phone_not_tappable"] = True
    if PARKED.search(d["text"][:4000]) and len(d["text"]) < 1500:
        _problem(a, "placeholder_page", "The page is a placeholder (coming soon / parked / under construction).", page.final_url)
    if d["flash"]:
        _problem(a, "uses_flash", "The page depends on Flash, which no current browser runs.", page.final_url)
    if len(d["text"]) < 200 and not PARKED.search(d["text"]):
        _problem(a, "very_little_content", "The home page has almost no readable text.", page.final_url)
    a.facts.update({"phones": d["phones"], "has_address": d["has_address"], "h1": d["h1"]})
    for e in set(d["mailtos"]) | set(d["emails"]):
        ce = clean_email(e)
        if ce:
            a.emails.append({"email": ce, "url": page.final_url})
    # a few internal links, to catch broken navigation
    checked = 0
    broken = []
    contact_page = None
    for href in d["links"]:
        full = _same_site(page.final_url, href)
        if not full:
            continue
        if contact_page is None and re.search(r"contact|about", full, re.I):
            contact_page = full
        if checked < link_sample and full != page.final_url:
            checked += 1
            try:
                sub = fetcher.get(full)
                a.pages.append(sub.final_url)
                if sub.status >= 400:
                    broken.append((full, sub.status))
                elif sub.html and not a.emails and re.search(r"contact", full, re.I):
                    for e in set(parse(sub.html)["mailtos"]) | set(parse(sub.html)["emails"]):
                        ce = clean_email(e)
                        if ce:
                            a.emails.append({"email": ce, "url": sub.final_url})
            except FetchError:
                pass                                           # an unreachable sub-page is not proof
    if broken:
        _problem(a, "broken_links", "Links on the home page lead to error pages: "
                 + "; ".join(f"{u} (HTTP {c})" for u, c in broken[:3]), page.final_url)
    a.facts["links_checked"] = checked
    return a


def same_business(page_text_phones: list[str], page_text: str, biz: dict) -> bool:
    """Does this site actually belong to this business? Phone match, or the exact name on the page."""
    want = digits(biz.get("phone", ""))
    if want and want in page_text_phones:
        return True
    n = " ".join((biz.get("name") or "").lower().split())
    return bool(n) and n in " ".join((page_text or "").lower().split())


def candidate_domain(email: str) -> str:
    """A site worth checking for a business whose published email is at its own domain."""
    e = clean_email(email)
    if not e:
        return ""
    dom = e.split("@", 1)[1]
    return "" if dom in FREE_MAIL else dom
