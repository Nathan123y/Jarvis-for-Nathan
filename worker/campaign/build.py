"""Build a one-page website concept from verified facts: a fixed design system, no generated code.

Why a deterministic renderer: it costs $0, runs unattended without any model or chat window, produces
the same trustworthy markup every time (so quality can be checked objectively), and it cannot invent
anything: every business detail on the page is a verified fact passed in, or a clearly marked sample.

Variation: six themes (palette + type) and layout options, chosen from the business category and a
stable hash of its name, so neighbouring businesses don't get identical pages.

Every page is labelled as an independent concept preview pending the owner's approval, is `noindex`,
loads nothing from other sites, has no script, and its contact form is shown as disabled.
"""
from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

CATEGORY_TITLES = {
    "plumber": "Plumbing", "electrician": "Electrical services", "hvac": "Heating and cooling", "roofer": "Roofing",
    "painter": "Painting", "carpenter": "Carpentry", "gardener": "Landscaping and gardening", "locksmith": "Locksmith",
    "tailor": "Tailoring and alterations", "photographer": "Photography", "hairdresser": "Hair salon", "barber": "Barber shop",
    "beauty": "Beauty salon", "car_repair": "Auto repair", "cleaning": "Cleaning and laundry", "dog_grooming": "Pet grooming",
}
CATEGORY_THEME = {
    "plumber": "steady", "electrician": "steady", "hvac": "steady", "roofer": "steady", "painter": "garden",
    "carpenter": "garden", "gardener": "garden", "locksmith": "garage", "car_repair": "garage",
    "hairdresser": "studio", "barber": "studio", "beauty": "studio", "cleaning": "fresh", "dog_grooming": "fresh",
    "tailor": "atelier", "photographer": "atelier",
}

# token -> hex. Every text/background pair used below is contrast-checked (check.py), not assumed.
THEMES = {
    "steady":  dict(bg="#FFFFFF", surface="#EEF3F8", text="#14212E", muted="#4A5B6C", primary="#0B4F8A", on_primary="#FFFFFF", accent="#B34700", font="system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif", head="Georgia,'Times New Roman',serif"),
    "garden":  dict(bg="#FBFDF9", surface="#EAF3E6", text="#1B2A1B", muted="#47604A", primary="#2F6B3A", on_primary="#FFFFFF", accent="#8A4B00", font="system-ui,-apple-system,'Segoe UI',Roboto,sans-serif", head="system-ui,-apple-system,'Segoe UI',sans-serif"),
    "garage":  dict(bg="#14171A", surface="#1F2428", text="#F2F4F5", muted="#B9C2C8", primary="#F2685A", on_primary="#14171A", accent="#FFC247", font="system-ui,-apple-system,'Segoe UI',Roboto,sans-serif", head="'Arial Narrow',Arial,sans-serif"),
    "studio":  dict(bg="#16120F", surface="#241D18", text="#F6EFE6", muted="#CDBFAE", primary="#D9A441", on_primary="#16120F", accent="#F2C879", font="'Helvetica Neue',Helvetica,Arial,sans-serif", head="Georgia,'Times New Roman',serif"),
    "fresh":   dict(bg="#FFFFFF", surface="#E7F6F5", text="#10302F", muted="#3F6463", primary="#0C7C79", on_primary="#FFFFFF", accent="#9A3D00", font="system-ui,-apple-system,'Segoe UI',Roboto,sans-serif", head="system-ui,-apple-system,'Segoe UI',sans-serif"),
    "atelier": dict(bg="#FBF7F1", surface="#F1E8DA", text="#2A1A1E", muted="#6A4F55", primary="#7A1F3D", on_primary="#FFFFFF", accent="#8B5A00", font="Georgia,'Times New Roman',serif", head="Georgia,'Times New Roman',serif"),
}
CONCEPT_NOTE = "Independent concept preview, not the official website"


@dataclass
class Site:
    files: dict
    theme: str
    layout: dict
    used: dict = field(default_factory=dict)       # the verified facts the page shows


def _h(s) -> str:
    return html.escape(str(s or ""), quote=True)


def pick_theme(category: str, name: str) -> str:
    return CATEGORY_THEME.get(category, "atelier" if not category else "steady")


def pick_layout(name: str) -> dict:
    n = int(hashlib.sha256((name or "").lower().encode()).hexdigest()[:6], 16)
    return {"hero": ("split", "center")[n % 2], "services": ("cards", "list")[(n >> 1) % 2], "nav_align": ("left", "right")[(n >> 2) % 2]}


def maps_url(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def tel_digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return ("+1" + d[-10:]) if len(d) >= 10 else ""


def css(t: dict, layout: dict) -> str:
    return f"""
:root{{--bg:{t['bg']};--surface:{t['surface']};--text:{t['text']};--muted:{t['muted']};--primary:{t['primary']};--on-primary:{t['on_primary']};--accent:{t['accent']}}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{margin:0;background:var(--bg);color:var(--text);font:1rem/1.6 {t['font']};overflow-wrap:anywhere}}
h1,h2,h3{{font-family:{t['head']};line-height:1.15;margin:0 0 .5em}}
h1{{font-size:clamp(2rem,6vw,3.4rem)}} h2{{font-size:clamp(1.5rem,4vw,2.1rem)}} h3{{font-size:1.15rem}}
p{{margin:0 0 1em}} a{{color:var(--primary)}}
.skip{{position:absolute;left:-9999px}} .skip:focus{{left:1rem;top:1rem;background:var(--surface);color:var(--text);padding:.6rem 1rem;z-index:9}}
.banner{{background:var(--surface);color:var(--text);padding:.7rem 1rem;text-align:center;font-size:.95rem;border-bottom:3px solid var(--accent)}}
.wrap{{max-width:68rem;margin:0 auto;padding:0 1.25rem}}
header .wrap{{display:flex;flex-wrap:wrap;gap:.75rem 1.5rem;align-items:center;justify-content:{'flex-end' if layout['nav_align']=='right' else 'space-between'};padding-top:1rem;padding-bottom:1rem}}
.brand{{font:700 1.2rem {t['head']};margin-right:auto}}
nav a{{display:inline-block;padding:.7rem .6rem;color:var(--text);text-decoration:none;font-weight:600}} nav a:hover{{text-decoration:underline}}
a:focus-visible,button:focus-visible{{outline:3px solid var(--accent);outline-offset:2px}}
.hero{{padding:3rem 0 3.5rem;background:linear-gradient(135deg,var(--surface),var(--bg) 70%)}}
.hero .wrap{{display:grid;gap:2rem;grid-template-columns:{'1.2fr 1fr' if layout['hero']=='split' else '1fr'};align-items:center;{'text-align:center;justify-items:center' if layout['hero']=='center' else ''}}}
.kicker{{color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase;font-size:.9rem}}
.actions{{display:flex;flex-wrap:wrap;gap:.75rem;margin-top:1.25rem}}
.btn{{display:inline-flex;align-items:center;min-height:2.9rem;padding:.7rem 1.3rem;border-radius:.6rem;font-weight:700;text-decoration:none;background:var(--primary);color:var(--on-primary);border:2px solid var(--primary)}}
.btn.alt{{background:transparent;color:var(--primary)}}
.art{{aspect-ratio:4/3;width:100%;max-width:26rem;border-radius:1rem;background:var(--primary);display:grid;place-items:center;color:var(--on-primary);font:700 4.5rem {t['head']};opacity:.95}}
section{{padding:3rem 0}} .alt-bg{{background:var(--surface)}}
.grid{{display:grid;gap:1.1rem;grid-template-columns:{'repeat(auto-fit,minmax(15rem,1fr))' if layout['services']=='cards' else '1fr'}}}
.card{{background:var(--surface);border-radius:.9rem;padding:1.3rem;border:1px solid color-mix(in srgb,var(--muted) 35%,transparent)}}
.alt-bg .card{{background:var(--bg)}}
.sample{{color:var(--muted);font-size:.95rem;margin:0}} .grid+.sample{{margin-top:1rem}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:.5rem 1.5rem;margin:0}} dt{{font-weight:700}} dd{{margin:0}}
.disabled{{border:2px dashed var(--muted);border-radius:.9rem;padding:1.2rem;color:var(--muted)}}
footer{{padding:2rem 0;border-top:1px solid var(--surface);color:var(--muted);font-size:.95rem}}
@media(max-width:640px){{.hero .wrap{{grid-template-columns:1fr}} dl{{grid-template-columns:1fr}} .art{{display:none}}}}
@media(prefers-reduced-motion:no-preference){{html{{scroll-behavior:smooth}}}}
"""


def render(biz: dict, facts: dict, *, sender: str, theme: Optional[str] = None) -> Site:
    """biz: name, category, city, address, phone, email (verified values only).
    facts: {"services": [..verified..], "hours": "...", "service_area": "..."}; any may be absent."""
    name = (biz.get("name") or "").strip()
    cat = biz.get("category") or ""
    tname = theme or pick_theme(cat, name)
    layout = pick_layout(name)
    t = THEMES[tname]
    title = CATEGORY_TITLES.get(cat, "Local business")
    city = (biz.get("city") or "").strip()
    phone, email, address = (biz.get("phone") or "").strip(), (biz.get("email") or "").strip(), (biz.get("address") or "").strip()
    tel = tel_digits(phone)
    services = [s for s in (facts.get("services") or []) if s][:6]
    used = {"name": name, "phone": phone if tel else "", "email": email, "address": address, "city": city,
            "services": services, "hours": facts.get("hours") or "", "category": title}

    actions = []
    if tel:
        actions.append(f'<a class="btn" href="tel:{_h(tel)}">Call {_h(phone)}</a>')
    if email:
        actions.append(f'<a class="btn alt" href="mailto:{_h(email)}">Email us</a>')
    if not actions:
        actions.append('<span class="sample">Contact details are being confirmed with the owner.</span>')

    if services:
        cards = "".join(f'<div class="card"><h3>{_h(s)}</h3></div>' for s in services)
        svc_note = ""
    else:
        cards = "".join(f'<div class="card"><h3>{_h(n)}</h3><p class="sample">Sample card. The owner\'s real services go here.</p></div>'
                        for n in ("First service", "Second service", "Third service"))
        svc_note = '<p class="sample">These cards are placeholders in this concept; nothing here is a claim about what the business offers.</p>'

    info = []
    if facts.get("hours"):
        info.append(f"<dt>Hours</dt><dd>{_h(facts['hours'])}</dd>")
    if address:
        info.append(f'<dt>Location</dt><dd>{_h(address)} <a href="{_h(maps_url(address))}">Get directions</a></dd>')
    elif city:
        info.append(f"<dt>Area</dt><dd>{_h(city)}</dd>")
    if tel:
        info.append(f'<dt>Phone</dt><dd><a href="tel:{_h(tel)}">{_h(phone)}</a></dd>')
    if email:
        info.append(f'<dt>Email</dt><dd><a href="mailto:{_h(email)}">{_h(email)}</a></dd>')
    info_html = "<dl>" + "".join(info) + "</dl>" if info else '<p class="sample">Details to be confirmed with the owner.</p>'

    banner = (f"{CONCEPT_NOTE} of {_h(name)}. Prepared by {_h(sender)} for the owner to look over. "
              "Nothing here is final, and it is not affiliated with the business.")
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{_h(name)} | concept preview</title>
<style>{css(t, layout)}</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="banner" role="note">{banner}</div>
<header><div class="wrap"><span class="brand">{_h(name)}</span><nav aria-label="Page sections"><a href="#services">Services</a><a href="#info">Info</a><a href="#contact">Contact</a></nav></div></header>
<main id="main">
<section class="hero"><div class="wrap"><div>
<p class="kicker">{_h(title)}{(' in ' + _h(city)) if city else ''}</p>
<h1>{_h(name)}</h1>
<p>{_h(title)}{(' serving ' + _h(city) + ' and nearby') if city else ''}.</p>
<div class="actions">{''.join(actions)}</div></div>
<div class="art" aria-hidden="true">{_h(name[:1].upper())}</div></div></section>
<section id="services"><div class="wrap"><h2>Services</h2><div class="grid">{cards}</div>{svc_note}</div></section>
<section id="info" class="alt-bg"><div class="wrap"><h2>Find us</h2>{info_html}</div></section>
<section id="contact"><div class="wrap"><h2>Contact</h2>
<div class="actions">{''.join(actions)}</div>
<p></p><div class="disabled" role="note"><strong>Contact form disabled.</strong> This concept does not collect or send messages. A working form or booking tool would be set up if the owner approves the site.</div></div></section>
</main>
<footer><div class="wrap"><p>{banner}</p><p>To have this preview removed, reply to the email it came with and it will be taken down.</p></div></footer>
</body>
</html>
"""
    return Site(files={"index.html": doc}, theme=tname, layout=layout, used=used)
