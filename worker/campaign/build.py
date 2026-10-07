"""Build a one-page website concept from verified facts: a fixed design system, no generated code.

Why a deterministic renderer: it costs $0, runs unattended without any model or chat window, produces
the same trustworthy markup every time (so quality can be checked objectively), and it cannot invent
anything: every business detail on the page is a verified fact passed in, or a clearly marked sample.

Design: six directions, each with its own composition rather than just its own colours, chosen from the
business's trade. Every page leads with the way a customer actually reaches a local business (the phone
number), carries a trade-specific illustration drawn inline as SVG, and keeps a call bar pinned to the
bottom of the phone screen. Nothing is loaded from other sites, there is no script, and nothing is
claimed that was not verified: suggested services, the about text and the page wording are labelled as
samples for the owner to replace.

Every page is labelled as an independent concept preview pending the owner's approval, is `noindex`,
and its contact form is shown as disabled.
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
# Typical services for the trade, shown only as labelled samples for the owner to replace.
SAMPLE_SERVICES = {
    "plumber": ["Leak repair", "Drain cleaning", "Water heaters", "Fixture installation"],
    "electrician": ["Panel upgrades", "Lighting installation", "Outlets and switches", "Troubleshooting"],
    "hvac": ["Heating repair", "Air conditioning", "System installation", "Seasonal tune-ups"],
    "roofer": ["Roof repair", "Roof replacement", "Gutters", "Inspections"],
    "painter": ["Interior painting", "Exterior painting", "Color consulting", "Trim and cabinets"],
    "carpenter": ["Custom woodwork", "Repairs", "Shelving and built-ins", "Doors and framing"],
    "gardener": ["Lawn care", "Planting and design", "Irrigation", "Seasonal clean-ups"],
    "locksmith": ["Lock changes", "Rekeying", "Lockouts", "Security hardware"],
    "tailor": ["Hemming", "Alterations", "Repairs", "Custom fitting"],
    "photographer": ["Portraits", "Events", "Business photos", "Prints"],
    "hairdresser": ["Cuts and styling", "Color", "Treatments", "Special occasions"],
    "barber": ["Haircuts", "Fades", "Beard trims", "Hot-towel shaves"],
    "beauty": ["Facials", "Nails", "Brows and lashes", "Skin care"],
    "car_repair": ["Diagnostics", "Brakes", "Oil changes", "Tires and alignment"],
    "cleaning": ["Home cleaning", "Office cleaning", "Laundry", "Move-in and move-out"],
    "dog_grooming": ["Baths", "Haircuts", "Nail trims", "De-shedding"],
}

# token -> hex. Every text/background pair used below is contrast-checked (check.py), not assumed.
THEMES = {
    "steady":  dict(bg="#FFFFFF", surface="#EAF0F6", text="#0F1B2A", muted="#45566A", primary="#0B4F8A", on_primary="#FFFFFF", accent="#B34700",
                    font="'Avenir Next','Segoe UI',system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif",
                    head="'Avenir Next Condensed','Franklin Gothic Medium','Arial Narrow','Helvetica Neue',Arial,sans-serif"),
    "garden":  dict(bg="#F7FAF2", surface="#E1EDD5", text="#17261A", muted="#425C46", primary="#2B6637", on_primary="#FFFFFF", accent="#7E4600",
                    font="'Avenir Next','Segoe UI',system-ui,-apple-system,Roboto,sans-serif",
                    head="'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif"),
    "garage":  dict(bg="#121518", surface="#1D2227", text="#F1F3F4", muted="#B4BEC5", primary="#FF6B57", on_primary="#121518", accent="#FFC247",
                    font="'Helvetica Neue',Helvetica,'Segoe UI',Arial,sans-serif",
                    head="'Impact','Haettenschweiler','Arial Narrow Bold','Arial Narrow',sans-serif"),
    "studio":  dict(bg="#15110E", surface="#231C17", text="#F6EFE6", muted="#CDBFAE", primary="#D9A441", on_primary="#15110E", accent="#F2C879",
                    font="'Helvetica Neue',Helvetica,Arial,sans-serif",
                    head="'Didot','Bodoni 72','Playfair Display',Georgia,'Times New Roman',serif"),
    "fresh":   dict(bg="#FFFFFF", surface="#E1F4F2", text="#0E2F2E", muted="#3D6261", primary="#0A7A77", on_primary="#FFFFFF", accent="#963A00",
                    font="'Avenir Next','Trebuchet MS','Segoe UI',system-ui,sans-serif",
                    head="'Avenir Next Rounded','Arial Rounded MT Bold','Trebuchet MS','Segoe UI',system-ui,sans-serif"),
    "atelier": dict(bg="#F6F4F8", surface="#E8E2EE", text="#1D1525", muted="#594A66", primary="#6B1D4E", on_primary="#FFFFFF", accent="#7C4F00",
                    font="'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif",
                    head="'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif"),
}
CONCEPT_NOTE = "Independent concept preview, not the official website"

# Line icons on a 48 x 48 grid (stroke only, drawn with currentColor).
ICONS = {
    "plumber": '<path d="M6 12h18a8 8 0 0 1 8 8v22"/><path d="M4 8h6v8H4zM28 40h8v4h-8z"/><path d="M40 6c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z"/>',
    "electrician": '<path d="M27 3 9 27h13l-3 18 20-26H26z"/>',
    "hvac": '<path d="M24 3v42M7 13l34 22M7 35l34-22"/><path d="M19 6l5 5 5-5M19 42l5-5 5 5"/>',
    "roofer": '<path d="M3 26 24 7l21 19"/><path d="M9 21v22h30V21"/><path d="M20 43V30h8v13"/>',
    "painter": '<path d="M7 6h28v13H7z"/><path d="M35 12h7v14H24v7"/><path d="M20 33h8v12h-8z"/>',
    "carpenter": '<path d="M24 9l17 7-3 8-17-7z"/><path d="M26 22 14 45"/>',
    "gardener": '<path d="M7 41C6 21 20 7 42 6c1 22-12 35-35 35z"/><path d="M7 41 29 19"/>',
    "locksmith": '<circle cx="14" cy="24" r="9"/><path d="M23 24h21M36 24v8M43 24v6"/>',
    "tailor": '<path d="M8 41 36 7"/><path d="M33 4a4 4 0 1 1 6 5"/><path d="M10 40c10 2 6-12 16-12s6 14 16 12"/>',
    "photographer": '<path d="M4 15h11l4-6h10l4 6h11v28H4z"/><circle cx="24" cy="28" r="8"/>',
    "hairdresser": '<circle cx="12" cy="37" r="6"/><circle cx="12" cy="11" r="6"/><path d="M17 15l27 22M17 33 44 11"/>',
    "barber": '<circle cx="12" cy="37" r="6"/><circle cx="12" cy="11" r="6"/><path d="M17 15l27 22M17 33 44 11"/>',
    "beauty": '<path d="M24 3l5 15 15 6-15 6-5 15-5-15-15-6 15-6z"/>',
    "car_repair": '<path d="M31 5a11 11 0 0 0-10 15L5 36l7 7 16-16a11 11 0 0 0 15-10l-7 5-5-5 5-7z"/>',
    "cleaning": '<circle cx="17" cy="31" r="12"/><circle cx="36" cy="14" r="7"/><circle cx="38" cy="37" r="4"/>',
    "dog_grooming": '<path d="M24 28c-7 0-12 5-12 11h24c0-6-5-11-12-11z"/><circle cx="10" cy="22" r="4"/><circle cx="19" cy="13" r="4"/><circle cx="29" cy="13" r="4"/><circle cx="38" cy="22" r="4"/>',
    "": '<path d="M5 20 24 5l19 15v23H5z"/><path d="M19 43V29h10v14"/>',
}


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
    return {"flip": bool(n % 2), "services": ("rows", "tiles")[(n >> 1) % 2], "shape": (n >> 2) % 3}


def maps_url(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def tel_digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    return ("+1" + d[-10:]) if len(d) >= 10 else ""


def icon(cat: str, size: int = 48, cls: str = "ico", width: float = 2.6) -> str:
    return (f'<svg class="{cls}" viewBox="0 0 48 48" width="{size}" height="{size}" fill="none" stroke="currentColor" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            f'{ICONS.get(cat, ICONS[""])}</svg>')


def art(theme: str, cat: str, name: str, shape: int) -> str:
    """The hero illustration: the trade's icon, large, set in a composition that belongs to the theme."""
    big = icon(cat, 220, "big", 2.2)
    if theme == "steady":
        return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
                f'<rect x="20" y="20" width="360" height="360" rx="28" fill="var(--primary)"/>'
                f'<circle cx="200" cy="200" r="150" stroke="var(--on-primary)" stroke-opacity=".35" stroke-width="2"/>'
                f'<circle cx="200" cy="200" r="104" stroke="var(--on-primary)" stroke-opacity=".5" stroke-width="2"/>'
                f'<rect x="20" y="318" width="360" height="62" rx="0" fill="var(--accent)"/>'
                f'<g transform="translate(90 80) scale(4.6)" color="var(--on-primary)" stroke="currentColor" stroke-width="2.3" '
                f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')
    if theme == "garden":
        blobs = ("M60 220c0-90 80-170 170-160s130 90 100 170-110 130-190 100S60 280 60 220z",
                 "M70 190c20-90 110-140 190-110s110 120 60 190-150 100-210 50S50 250 70 190z",
                 "M50 200c0-80 70-150 160-150s150 80 140 160-80 140-170 130S50 280 50 200z")[shape % 3]
        return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
                f'<path d="{blobs}" fill="var(--primary)"/><circle cx="318" cy="92" r="46" fill="var(--accent)"/>'
                f'<g transform="translate(95 95) scale(4.2)" color="var(--bg)" stroke="currentColor" stroke-width="2.4" '
                f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')
    if theme == "garage":
        return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
                f'<rect x="0" y="0" width="400" height="400" fill="var(--surface)"/>'
                f'<path d="M-20 300 420 120v60L-20 360z" fill="var(--accent)"/><path d="M-20 360 420 180v40L-20 400z" fill="var(--primary)"/>'
                f'<g transform="translate(88 40) scale(4.4)" color="var(--text)" stroke="currentColor" stroke-width="2.4" '
                f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')
    if theme == "studio":
        return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
                f'<circle cx="200" cy="200" r="176" stroke="var(--primary)" stroke-width="2"/>'
                f'<circle cx="200" cy="200" r="158" stroke="var(--primary)" stroke-opacity=".5" stroke-width="1"/>'
                f'<g transform="translate(104 104) scale(4)" color="var(--primary)" stroke="currentColor" stroke-width="1.8" '
                f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')
    if theme == "fresh":
        dots = "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="var(--bg)" fill-opacity=".55"/>'
                       for x, y, r in ((80, 90, 22), (330, 70, 30), (350, 300, 18), (60, 320, 34), (310, 190, 12), (120, 40, 10)))
        return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
                f'<rect x="10" y="10" width="380" height="380" rx="190" fill="var(--primary)"/>{dots}'
                f'<g transform="translate(95 95) scale(4.2)" color="var(--on-primary)" stroke="currentColor" stroke-width="2.4" '
                f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')
    initial = _h((name or "?").strip()[:1])
    return (f'<div class="art" aria-hidden="true"><svg viewBox="0 0 400 400" width="400" height="400" fill="none">'
            f'<rect x="30" y="30" width="340" height="340" fill="var(--surface)"/><rect x="30" y="30" width="340" height="340" stroke="var(--primary)" stroke-width="2"/>'
            f'<text x="200" y="300" text-anchor="middle" font-size="300" font-style="italic" fill="var(--primary)" '
            f'font-family="Iowan Old Style,Palatino,Georgia,serif">{initial}</text>'
            f'<g transform="translate(280 52) scale(1.6)" color="var(--accent)" stroke="currentColor" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round" fill="none">{ICONS.get(cat, ICONS[""])}</g></svg></div>')


def css(t: dict, theme: str, layout: dict) -> str:
    dark = theme in ("garage", "studio")
    head_w = {"steady": 800, "garden": 700, "garage": 400, "studio": 400, "fresh": 800, "atelier": 400}[theme]
    head_ls = {"garage": ".01em", "studio": "-.01em", "atelier": "-.02em"}.get(theme, "-.015em")
    centered = theme in ("studio",)
    return f"""
:root{{--bg:{t['bg']};--surface:{t['surface']};--text:{t['text']};--muted:{t['muted']};--primary:{t['primary']};--on-primary:{t['on_primary']};--accent:{t['accent']}}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{margin:0;background:var(--bg);color:var(--text);font:1.0625rem/1.65 {t['font']};overflow-wrap:anywhere;padding-bottom:4.5rem}}
h1,h2,h3{{font-family:{t['head']};font-weight:{head_w};letter-spacing:{head_ls};line-height:1.05;margin:0 0 .45em}}
h1{{font-size:clamp(2.6rem,8.5vw,5.6rem)}} h2{{font-size:clamp(1.8rem,4.6vw,2.7rem)}} h3{{font-size:1.25rem;line-height:1.2}}
p{{margin:0 0 1em;max-width:62ch}} a{{color:var(--primary)}}
.skip{{position:absolute;left:-9999px}} .skip:focus{{left:1rem;top:1rem;background:var(--surface);color:var(--text);padding:.6rem 1rem;z-index:9}}
.banner{{background:var(--surface);color:var(--text);padding:.6rem 1rem;text-align:center;font-size:.9rem;line-height:1.45;border-bottom:3px solid var(--accent)}}
.wrap{{max-width:72rem;margin:0 auto;padding:0 1.25rem}}
header .wrap{{display:flex;flex-wrap:wrap;gap:.5rem 1.25rem;align-items:center;padding-top:.9rem;padding-bottom:.9rem}}
.brand{{font:{head_w} 1.3rem/1.1 {t['head']};margin-right:auto;letter-spacing:{head_ls}}}
nav{{display:flex;gap:.25rem}}
nav a{{padding:.7rem .7rem;color:var(--text);text-decoration:none;font-weight:600}} nav a:hover{{text-decoration:underline;text-underline-offset:.3em}}
a:focus-visible{{outline:3px solid var(--accent);outline-offset:3px}}
.btn{{display:inline-flex;align-items:center;gap:.6rem;min-height:3.1rem;padding:.7rem 1.4rem;border-radius:{'.3rem' if theme in ('garage','atelier') else '999px' if theme in ('fresh','studio') else '.55rem'};font-weight:800;text-decoration:none;background:var(--primary);color:var(--on-primary);border:2px solid var(--primary)}}
.btn.alt{{background:transparent;color:var(--primary)}}
.btn:hover{{filter:brightness(1.08)}}
.hero{{padding:clamp(2.2rem,6vw,4.5rem) 0 clamp(2.5rem,6vw,4.5rem);{'background:var(--surface);' if theme in ('steady','fresh') else ''}}}
.hero .wrap{{display:grid;gap:2.5rem;grid-template-columns:{'1fr' if centered else '1.15fr .85fr'};align-items:center;{'text-align:center;justify-items:center' if centered else ''}{';direction:rtl' if layout['flip'] and theme in ('steady','garden','fresh') else ''}}}
.hero .wrap>*{{direction:ltr}}
.where{{color:var(--muted);font-weight:600;margin:0 0 .9rem}}
.lede{{font-size:1.25rem;color:var(--muted);margin:.4rem 0 1.4rem}}
.phone{{display:inline-block;font:{head_w} clamp(1.9rem,5.5vw,3rem)/1 {t['head']};color:var(--primary);text-decoration:none;letter-spacing:{head_ls};margin:.2rem 0 1.1rem}}
.actions{{display:flex;flex-wrap:wrap;gap:.75rem;margin-top:.4rem{';justify-content:center' if centered else ''}}}
.art{{width:100%;max-width:25rem;justify-self:center}} .art svg{{width:100%;height:auto;display:block}}
{'.stripe{height:.9rem;background:repeating-linear-gradient(-45deg,var(--accent) 0 1.1rem,var(--bg) 1.1rem 2.2rem)}' if theme=='garage' else ''}
section{{padding:clamp(2.5rem,6vw,4.5rem) 0}} .alt-bg{{background:var(--surface)}}
.sec-head{{margin-bottom:1.6rem}} .sec-head p{{color:var(--muted);margin:0}}
.services{{display:grid;gap:{'1rem' if layout['services']=='tiles' else '0'};grid-template-columns:{'repeat(auto-fit,minmax(14rem,1fr))' if layout['services']=='tiles' else '1fr'};list-style:none;margin:0;padding:0}}
.services li{{display:flex;align-items:center;gap:1rem;{'padding:1.25rem;border-radius:1rem;background:var(--surface);flex-direction:column;align-items:flex-start;text-align:left' if layout['services']=='tiles' else 'padding:1.1rem 0;border-top:1px solid color-mix(in srgb,var(--muted) 40%,transparent)'}}}
.alt-bg .services li{{{'background:var(--bg)' if layout['services']=='tiles' else ''}}}
.services li:last-child{{{'' if layout['services']=='tiles' else 'border-bottom:1px solid color-mix(in srgb,var(--muted) 40%,transparent)'}}}
.services .ico{{flex:none;color:var(--primary);width:2.6rem;height:2.6rem}}
.services h3{{margin:0;font-size:1.3rem}}
.sample{{color:var(--muted);font-size:.95rem;margin:1rem 0 0}}
.about{{display:grid;gap:2rem;grid-template-columns:1fr 1fr;align-items:start}}
.facts{{display:grid;grid-template-columns:max-content 1fr;gap:.9rem 1.75rem;margin:0}} .facts dt{{font-weight:800}} .facts dd{{margin:0}}
.call-band{{background:var(--primary);color:var(--on-primary);padding:clamp(2.2rem,5vw,3.5rem) 0}}
.call-band h2{{margin-bottom:.3em}} .call-band a{{color:var(--on-primary)}}
.call-band .phone{{color:var(--on-primary)}} .call-band .actions{{justify-content:flex-start}}
.call-band .btn{{background:var(--on-primary);color:var(--primary);border-color:var(--on-primary)}}
.call-band .btn.alt{{background:transparent;color:var(--on-primary)}}
.disabled{{border:2px dashed var(--on-primary);border-radius:.9rem;padding:1rem 1.2rem;margin-top:1.4rem;max-width:44rem}}
footer{{padding:2rem 0 1.5rem;color:var(--muted);font-size:.92rem}} footer p{{margin:0 0 .6rem}}
.callbar{{position:fixed;left:0;right:0;bottom:0;z-index:5;display:none;padding:.6rem .8rem;background:var(--bg);border-top:1px solid color-mix(in srgb,var(--muted) 45%,transparent)}}
.callbar .btn{{width:100%;justify-content:center}}
@media(max-width:760px){{.hero .wrap,.about{{grid-template-columns:1fr}} .facts{{grid-template-columns:1fr;gap:.2rem}} .facts dd{{margin-bottom:.8rem}}
.callbar{{display:block}} nav{{display:none}} .art{{max-width:17rem;order:-1}} header .wrap{{padding-top:.7rem;padding-bottom:.7rem}}}}
@media(min-width:761px){{body{{padding-bottom:0}}}}
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

    place = f"{title} in {city}" if city else title
    actions = []
    if email:
        actions.append(f'<a class="btn alt" href="mailto:{_h(email)}">Email {_h(name)}</a>')
    contact_line = ""
    if tel:
        contact_line = f'<a class="phone" href="tel:{_h(tel)}">{_h(phone)}</a>'
    else:
        contact_line = '<p class="lede">Contact details are being confirmed with the owner.</p>'
    callbar = (f'<div class="callbar"><a class="btn" href="tel:{_h(tel)}">Call {_h(phone)}</a></div>'
               if tel else "")

    if services:
        items, svc_note = services, ""
        items_html = "".join(f'<li>{icon(cat, 44)}<h3>{_h(s)}</h3></li>' for s in items)
    else:
        items = SAMPLE_SERVICES.get(cat) or ["Service one", "Service two", "Service three"]
        items_html = "".join(f'<li>{icon(cat, 44)}<h3>{_h(s)}</h3></li>' for s in items)
        svc_note = ('<p class="sample">Sample list. These are typical services for this kind of business, shown here as placeholders; '
                    'the owner chooses the real ones.</p>')

    info = []
    if facts.get("hours"):
        info.append(f"<dt>Hours</dt><dd>{_h(facts['hours'])}</dd>")
    if address:
        info.append(f'<dt>Find us</dt><dd>{_h(address)}<br><a href="{_h(maps_url(address))}">Get directions</a></dd>')
    elif city:
        info.append(f"<dt>Area</dt><dd>{_h(city)}</dd>")
    if tel:
        info.append(f'<dt>Phone</dt><dd><a href="tel:{_h(tel)}">{_h(phone)}</a></dd>')
    if email:
        info.append(f'<dt>Email</dt><dd><a href="mailto:{_h(email)}">{_h(email)}</a></dd>')
    info_html = '<dl class="facts">' + "".join(info) + "</dl>" if info else '<p class="sample">Details to be confirmed with the owner.</p>'

    about = (f'<p>This is where the story of {_h(name)} goes: who runs it, what the work looks like, and why neighbors '
             f'{"in " + _h(city) + " " if city else ""}pick up the phone.</p>'
             '<p class="sample">Sample text. The owner writes this part, or we draft it together once the site is approved.</p>')

    banner = (f"{CONCEPT_NOTE} of {_h(name)}. Prepared by {_h(sender)} for the owner to look over. "
              "Nothing here is final, and it is not affiliated with the business.")
    stripe = '<div class="stripe" aria-hidden="true"></div>' if tname == "garage" else ""
    hero_text = (f'<div><p class="where">{_h(place)}</p><h1>{_h(name)}</h1>'
                 f'<p class="lede">{_h(title)}{(", serving " + _h(city) + " and nearby") if city else ""}.</p>'
                 f'{contact_line}<div class="actions">{"".join(actions)}</div></div>')
    hero_art = art(tname, cat, name, layout["shape"])
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{_h(name)} | concept preview</title>
<style>{css(t, tname, layout)}</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="banner" role="note">{banner}</div>
<header><div class="wrap"><span class="brand">{_h(name)}</span><nav aria-label="Page sections"><a href="#services">Services</a><a href="#about">About</a><a href="#info">Find us</a><a href="#contact">Contact</a></nav></div></header>
{stripe}
<main id="main">
<section class="hero"><div class="wrap">{hero_text}{hero_art}</div></section>
<section id="services"><div class="wrap"><div class="sec-head"><h2>What we do</h2></div><ul class="services">{items_html}</ul>{svc_note}</div></section>
<section id="about" class="alt-bg"><div class="wrap about"><div><h2>About</h2>{about}</div><div id="info"><h2>Find us</h2>{info_html}</div></div></section>
<section id="contact" class="call-band"><div class="wrap"><h2>{'Call today' if tel else 'Get in touch'}</h2>
{contact_line}
<div class="actions">{"".join(a.replace('btn alt', 'btn') for a in actions)}</div>
<div class="disabled" role="note"><strong>Contact form disabled.</strong> This concept does not collect or send messages. A working form or booking tool would be set up if the owner approves the site.</div></div></section>
</main>
<footer><div class="wrap"><p>{banner}</p><p>To have this preview removed, reply to the email it came with and it will be taken down.</p></div></footer>
{callbar}
</body>
</html>
"""
    return Site(files={"index.html": doc}, theme=tname, layout=layout, used=used)
