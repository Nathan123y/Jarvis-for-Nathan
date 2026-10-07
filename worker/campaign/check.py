"""Objective quality checks for a built site. A concept that fails any required check is held, not sent.

Static checks (always run): structure, accessibility basics, contrast of every text/background pair
computed from the theme colours, no outside requests or scripts, concept label and noindex present,
contact form disabled, and *content accuracy*: every business detail shown matches a verified fact and
nothing that sounds like an unverifiable claim (awards, reviews, licences, guarantees, years in business)
appears unless it was supplied as a verified fact.

Browser check (when Playwright and Chromium are installed): renders at phone and desktop widths,
fails on horizontal overflow or tiny text, and saves screenshots to look at. Without a browser the
check reports "not_run" and the campaign policy decides whether that is good enough (default: no).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from worker.campaign.build import CONCEPT_NOTE, THEMES

CLAIM_WORDS = re.compile(r"\b(award|award-winning|best|#1|number one|top[- ]rated|guarantee[d]?|licensed|insured|bonded|"
                         r"certified|years? (of )?experience|since \d{4}|family[- ]owned|testimonial|reviews?|five[- ]star|"
                         r"5[- ]star|free estimate|24/7|same[- ]day|emergency)\b", re.I)


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


PAIRS = (("text", "bg"), ("text", "surface"), ("muted", "bg"), ("muted", "surface"), ("on_primary", "primary"),
         ("primary", "bg"), ("primary", "surface"), ("accent", "surface"))


class _Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text, self.ids, self.hrefs, self._skip = [], [], [], set(), [], 0
        self.title, self._t = "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        self.attrs.append((tag, a))
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag == "title":
            self._t = True
        if tag in ("style", "script"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._t = False
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, d):
        if self._t:
            self.title += d
        elif not self._skip and d.strip():
            self.text.append(d.strip())


def _r(name: str, ok: bool, detail: str = "", required: bool = True) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, "required": required}


def static_checks(html: str, theme: str, used: dict, sender: str) -> list[dict]:
    d = _Doc()
    d.feed(html)
    text = " ".join(d.text)
    res = []
    res.append(_r("doctype_and_lang", html.lstrip().lower().startswith("<!doctype html>") and 'lang="en"' in html))
    res.append(_r("title", bool(d.title.strip()) and used["name"].lower() in d.title.lower(), d.title))
    res.append(_r("viewport", any(t == "meta" and a.get("name") == "viewport" and "width=device-width" in (a.get("content") or "") for t, a in d.attrs)))
    res.append(_r("noindex", any(t == "meta" and a.get("name") == "robots" and "noindex" in (a.get("content") or "") for t, a in d.attrs)))
    res.append(_r("one_h1", d.tags.count("h1") == 1, f"{d.tags.count('h1')} h1 elements"))
    res.append(_r("landmarks", all(x in d.tags for x in ("header", "main", "footer", "nav"))))
    res.append(_r("skip_link", "skip" in html and "#main" in d.hrefs and "main" in d.ids))
    res.append(_r("concept_label", text.lower().count(CONCEPT_NOTE.lower()) >= 2 and sender.lower() in text.lower(),
                  "banner and footer both label it an independent concept"))
    res.append(_r("no_scripts", "script" not in d.tags))
    ext = [a.get(k) for t, a in d.attrs for k in ("src", "href") if a.get(k, "").startswith(("http://", "https://", "//"))
           and t != "a"]
    res.append(_r("no_outside_resources", not ext and "@import" not in html and "url(" not in html, ", ".join(map(str, ext))[:200]))
    out_links = [h for h in d.hrefs if h.startswith(("http://", "https://")) and not h.startswith("https://www.google.com/maps/search/")]
    res.append(_r("only_expected_outside_links", not out_links, ", ".join(out_links)[:200]))
    res.append(_r("forms_disabled", "form" not in d.tags and "input" not in d.tags and "button" not in d.tags and "disabled" in text.lower()))
    bad_anchor = [h for h in d.hrefs if h.startswith("#") and h[1:] not in d.ids]
    res.append(_r("anchors_resolve", not bad_anchor, ", ".join(bad_anchor)))
    t = THEMES[theme]
    low = [f"{a}/{b}={contrast(t[a], t[b]):.1f}" for a, b in PAIRS if contrast(t[a], t[b]) < 4.5]
    res.append(_r("contrast_wcag_aa", not low, ", ".join(low) or "all text pairs >= 4.5"))
    res.append(_r("page_weight", len(html.encode()) < 60_000, f"{len(html.encode())} bytes"))
    # accuracy
    tels = [h[4:] for h in d.hrefs if h.startswith("tel:")]
    want = re.sub(r"\D", "", used.get("phone") or "")[-10:]
    shown = [re.sub(r"\D", "", m)[-10:] for m in re.findall(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}", text)]
    res.append(_r("phone_matches_verified", all(re.sub(r"\D", "", x)[-10:] == want for x in tels) and (bool(tels) == bool(want))
                  and all(x == want for x in shown), f"shown {tels or shown} verified {want or 'none'}"))
    mails = [h[7:] for h in d.hrefs if h.startswith("mailto:")]
    seen_mail = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    res.append(_r("email_matches_verified", all(x == used.get("email") for x in mails + seen_mail)
                  and (bool(mails) == bool(used.get("email")))))
    res.append(_r("name_present", used["name"] in text))
    allowed = " ".join(str(v) for v in (used.get("services") or [])) + " " + str(used.get("hours") or "")
    # the sample/placeholder wording is ours; check only text outside our own fixed phrases
    scrub = text.replace(CONCEPT_NOTE, "")
    claims = [m.group(0) for m in CLAIM_WORDS.finditer(scrub) if m.group(0).lower() not in allowed.lower()]
    res.append(_r("no_unverified_claims", not claims, ", ".join(sorted(set(claims)))))
    return res


def visual_check(html_path: Path, out_dir: Path) -> dict:
    """Phone and desktop render. {"status": passed|failed|not_run, "detail", "screens": [...]}"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"status": "not_run", "detail": "Playwright is not installed", "screens": []}
    out_dir.mkdir(parents=True, exist_ok=True)
    problems, screens = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for label, w, h in (("mobile", 375, 812), ("desktop", 1280, 800)):
                    page = browser.new_page(viewport={"width": w, "height": h})
                    page.goto(html_path.resolve().as_uri())
                    sw = page.evaluate("document.documentElement.scrollWidth")
                    if sw > w + 1:
                        problems.append(f"{label}: sideways scroll ({sw}px wide in a {w}px window)")
                    small = page.evaluate("Math.min(...[...document.querySelectorAll('p,li,a,dd,dt,h1,h2,h3,span')]"
                                          ".filter(e=>e.innerText&&e.innerText.trim()).map(e=>parseFloat(getComputedStyle(e).fontSize)))")
                    if small < 13:
                        problems.append(f"{label}: text as small as {small}px")
                    shot = out_dir / f"{label}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    screens.append(str(shot))
                    page.close()
            finally:
                browser.close()
    except Exception as exc:
        return {"status": "not_run", "detail": f"browser could not run: {type(exc).__name__}", "screens": screens}
    return {"status": "failed" if problems else "passed", "detail": "; ".join(problems) or "no overflow, readable text", "screens": screens}


def verdict(static: list[dict], visual: Optional[dict], require_visual: bool) -> tuple[bool, str]:
    """(passes, why not). Required static failures always hold the site; so does a failed browser
    check; a browser check that did not run holds it unless the policy allows that."""
    failed = [r for r in static if r["required"] and not r["ok"]]
    if failed:
        return False, "failed checks: " + ", ".join(f"{r['check']} ({r['detail']})" if r["detail"] else r["check"] for r in failed)
    if visual and visual["status"] == "failed":
        return False, "browser check failed: " + visual["detail"]
    if require_visual and (not visual or visual["status"] != "passed"):
        return False, "browser check did not run (" + ((visual or {}).get("detail") or "not attempted") + ")"
    return True, ""
