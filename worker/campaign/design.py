"""An AI-designed concept site, held to the same rules as the template one.

Gemini (free tier, the key Jarvis already has) is asked to design a one-page site for ONE business from its
verified facts. What comes back is never trusted:

  * it must be plain HTML and CSS: a strict allowlist of tags, attributes and links, no script, no images,
    no forms, no outside requests, no colour that is not one of seven named tokens, no gradients;
  * we write the parts that carry legal and safety weight ourselves (the "independent concept" banner and
    footer, the disabled-contact-form note, the noindex tag, the phone bar), around the model's page;
  * the same static checks as the template run on the result (phone and email match the verified ones, no
    invented awards, reviews, licences or guarantees), plus a browser render and a measurement of the real
    contrast of every piece of text.

If the model is unavailable, over its free quota, or its page fails any of that twice, the campaign uses the
template design instead. Only public business details are sent to the model: never the sender's name,
postal address or anything about the person running the campaign.
"""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Callable, Optional

from worker.campaign import build

TOKENS = ("bg", "surface", "text", "muted", "primary", "on-primary", "accent")
# Newest first. A model name this account does not have is skipped, not fatal.
MODEL_LADDER = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash")
MAX_TOKENS = 16000
MAX_PAGE_BYTES = 55_000
TRIES = 2

DIRECTIONS = (
    "a bold poster: huge confident type, strong blocks of flat colour, one memorable graphic idea",
    "calm and editorial: generous space, a refined serif, thin rules, restrained colour",
    "warm and crafted: friendly rounded shapes, an earthy palette, hand-made feeling without images",
    "crisp and technical: a tight grid, a clear hierarchy, confident sans-serif, sharp corners",
    "playful and bright: rounded forms, saturated flat colour, big friendly headings",
    "dark and premium: a deep background, one luminous accent colour, elegant spacing",
)

ALLOWED_TAGS = {"header", "nav", "main", "section", "article", "aside", "div", "span", "p", "h1", "h2", "h3", "h4", "ul", "ol",
                "li", "a", "strong", "em", "br", "dl", "dt", "dd", "address", "small", "svg", "path", "circle", "rect", "line",
                "polyline", "polygon", "ellipse", "g", "title", "desc", "text"}
SVG_TAGS = {"svg", "path", "circle", "rect", "line", "polyline", "polygon", "ellipse", "g", "title", "desc", "text"}
ALLOWED_ATTRS = {"class", "id", "href", "role", "lang", "viewbox", "width", "height", "fill", "stroke", "stroke-width", "stroke-linecap",
                 "stroke-linejoin", "d", "cx", "cy", "r", "x", "y", "x1", "y1", "x2", "y2", "rx", "ry", "points", "transform",
                 "focusable", "preserveaspectratio", "text-anchor", "font-size", "font-weight", "font-family", "font-style",
                 "xmlns", "fill-rule", "clip-rule", "stroke-miterlimit"}
COLOR_ATTRS = {"fill", "stroke"}
MAPS_PREFIX = "https://www.google.com/maps/search/?api=1&query="
NAMED_COLORS = set("""aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood
cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey
darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey
darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon
lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue
lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid
mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin
navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff
peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver
skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
yellowgreen""".split())
BANNED_CSS = re.compile(r"@import|@font-face|url\s*\(|expression\s*\(|javascript:|gradient\s*\(|color-mix\s*\(|image-set|element\s*\(|"
                        r"\bopacity\s*:|behavior\s*:|-moz-binding|@namespace|@charset|\bfilter\s*:|mix-blend-mode|backdrop-filter", re.I)
COLOR_PROP = re.compile(r"(?:^|;|\{)\s*(color|background|background-color|border|border-top|border-right|border-bottom|border-left|"
                        r"border-color|outline|outline-color|fill|stroke|box-shadow|text-shadow|text-decoration|text-decoration-color|"
                        r"caret-color|accent-color|column-rule|stop-color)\s*:\s*([^;}{]+)", re.I)


def seed_direction(name: str) -> str:
    n = int(hashlib.sha256((name or "").lower().encode()).hexdigest()[:6], 16)
    return DIRECTIONS[n % len(DIRECTIONS)]


def prompt(biz: dict, facts: dict, *, direction: str, problems: Optional[list] = None) -> str:
    cat = biz.get("category") or ""
    title = build.CATEGORY_TITLES.get(cat, "Local business")
    sample = build.SAMPLE_SERVICES.get(cat) or []
    lines = [
        "You are the design lead at a small studio known for distinctive local-business websites. Design ONE complete "
        "one-page website for the business below. It is a concept preview the owner will see first, so it must look "
        "genuinely good on a phone and on a desktop, and feel specific to this trade, not like a template.",
        "",
        "VERIFIED FACTS (the only facts you may state about this business):",
        f"- Name: {biz.get('name')}",
        f"- Trade: {title}",
        f"- City: {biz.get('city') or '(unknown)'}",
        f"- Address: {biz.get('address') or '(none)'}",
        f"- Phone: {biz.get('phone') or '(none)'}",
        f"- Email: {biz.get('email') or '(none)'}",
        f"- Hours: {facts.get('hours') or '(none)'}",
        f"- Verified services: {', '.join(facts.get('services') or []) or '(none)'}",
        "",
        f"VISUAL DIRECTION: {direction}.",
        "Pick your own palette and type that suit this trade and this direction. Be deliberate: avoid the generic default look "
        "(cream background with a serif and a terracotta accent; near-black with one neon accent; identical rounded cards "
        "with the same soft shadow; all-caps tracked labels above every heading). Spend your boldness in ONE place (a striking "
        "hero) and keep the rest quiet and well-spaced. Use one or two typefaces from system stacks only.",
        "",
        "CONTENT RULES (a checker enforces these and rejects the page otherwise):",
        "- Use only the verified facts above. Never invent reviews, ratings, testimonials, awards, licences, insurance, "
        "guarantees, years in business, prices, team names, certifications, 'family-owned', 'best', '24/7', 'same-day', "
        "'emergency' or 'free estimate'.",
        "- Services: if verified services exist, list those. Otherwise you may show these typical services, and you must say "
        f"plainly near them that they are samples the owner will replace: {', '.join(sample) or 'a short generic list'}.",
        "- An About section is fine, but write it as a clearly labelled sample for the owner to replace, with no factual claims.",
        "- The phone number must appear as a tel: link and the email as a mailto: link, exactly as given. Add a directions "
        f"link using exactly this form: {MAPS_PREFIX}<url-encoded address>.",
        "- Do not write a banner, footer, cookie notice, contact form or any 'preview' wording: those are added for you.",
        "",
        "TECHNICAL RULES (a checker enforces these):",
        "- Output ONLY two parts, in this exact format, with no commentary and no markdown fences:",
        "===CSS===",
        "(all CSS. Start with :root{--bg:#...;--surface:#...;--text:#...;--muted:#...;--primary:#...;--on-primary:#...;--accent:#...}. "
        "These seven custom properties are the ONLY place a colour may be written. Everywhere else use var(--bg), var(--surface), "
        "var(--text), var(--muted), var(--primary), var(--on-primary), var(--accent), or transparent / currentColor.)",
        "===BODY===",
        "(the HTML that goes inside <body>: a <header> containing a <nav>, then <main id=\"main\"> with your sections, ending "
        "before any footer. Exactly one <h1> containing the business name.)",
        "- No <script>, <img>, <iframe>, <form>, <input>, <button>, <link>, <style> inside the body, no inline style attributes, "
        "no on* attributes, no url(), @import, gradients, opacity, filters, color-mix or box-shadow colours other than the tokens.",
        "- Pictures: none. Make the visual interest from type, flat colour shapes, CSS and inline <svg> illustration drawn "
        "from simple paths that suit the trade. SVG fill and stroke may only be none, currentColor or var(--token).",
        "- Responsive: mobile first, no horizontal scroll at 375px wide, body text at least 16px, buttons at least 44px tall. "
        "Keep the whole answer under 40 KB.",
        "- Every text colour must be readable on the colour behind it (at least 4.5:1, or 3:1 for text 24px or larger).",
        "- Include a visible keyboard focus style. Respect prefers-reduced-motion if you animate anything (prefer not to).",
    ]
    if problems:
        lines += ["", "YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix exactly these problems and keep what was good:"]
        lines += [f"- {x}" for x in problems[:10]]
    return "\n".join(lines)


def split_reply(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-z]*\n|\n```$", "", raw)
    m = re.search(r"===CSS===\s*(.*?)\s*===BODY===\s*(.*)$", raw, re.S)
    if not m:
        return "", ""
    css, body = m.group(1).strip(), m.group(2).strip()
    css = re.sub(r"^```[a-z]*\n|\n```$", "", css).strip()
    body = re.sub(r"^```[a-z]*\n|\n```$", "", body).strip()
    return css, body


def parse_tokens(css: str) -> dict:
    m = re.search(r":root\s*\{([^}]*)\}", css)
    out = {}
    if m:
        for name, val in re.findall(r"--([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;?", m.group(1)):
            if name in TOKENS:
                out[name.replace("-", "_")] = val.upper()
    return out


def css_problems(css: str) -> list[str]:
    bad = []
    if len(css) < 400:
        bad.append("the CSS is too short to be a real design")
    toks = parse_tokens(css)
    missing = [t for t in TOKENS if t.replace("-", "_") not in toks]
    if missing:
        bad.append("the :root block must define exactly these colours as 6-digit hex: --" + ", --".join(TOKENS) + f" (missing {', '.join(missing)})")
    if BANNED_CSS.search(css):
        bad.append(f"banned CSS construct: {BANNED_CSS.search(css).group(0)!r}")
    if "<" in css or "</" in css:
        bad.append("the CSS contains markup")
    rest = re.sub(r":root\s*\{[^}]*\}", "", css, count=1)
    if re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", re.sub(r"[#.][A-Za-z_][\w-]*(?=[\s,{:.>#\[+~)])", "", rest)) and re.search(
            r":\s*[^;{}]*#[0-9a-fA-F]{3,8}\b", rest):
        bad.append("a colour is written outside the :root tokens (use var(--token))")
    if re.search(r"\b(rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\s*\(", rest, re.I):
        bad.append("a colour function is used outside the :root tokens (use var(--token))")
    for prop, val in COLOR_PROP.findall(rest):
        for word in re.findall(r"[a-zA-Z]+", re.sub(r"var\([^)]*\)", " ", val)):
            if word.lower() in NAMED_COLORS:
                bad.append(f"the colour name '{word}' is used in {prop} (use var(--token))")
                break
    return bad


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems, self.tags = [], []
        self._svg = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "svg":
            self._svg += 1
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"the tag <{tag}> is not allowed")
            return
        if tag in SVG_TAGS and tag not in ("title",) and not self._svg:
            self.problems.append(f"<{tag}> must be inside an <svg>")
        for k, v in attrs:
            k = (k or "").lower()
            v = v or ""
            if k.startswith("on") or k == "style":
                self.problems.append(f"the attribute {k} is not allowed (use classes)")
            elif k not in ALLOWED_ATTRS and not k.startswith("aria-"):
                self.problems.append(f"the attribute {k} is not allowed")
            elif k in COLOR_ATTRS and v.lower() not in ("none", "currentcolor") and not re.fullmatch(r"var\(--[a-z-]+\)", v.lower()):
                self.problems.append(f"{k}=\"{v}\": use none, currentColor or var(--token)")
            elif k == "href" and tag == "a":
                if not (v.startswith(("tel:+", "mailto:", "#")) or v.startswith(MAPS_PREFIX)):
                    self.problems.append(f"link '{v[:60]}' is not allowed (only tel:, mailto:, #section and the Google Maps directions link)")
            elif k == "href":
                self.problems.append("href is only allowed on <a>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "svg":
            self._svg -= 1

    def handle_endtag(self, tag):
        if tag == "svg" and self._svg:
            self._svg -= 1


def body_problems(body: str) -> list[str]:
    bad = []
    if "<main" not in body or 'id="main"' not in body:
        bad.append('the body needs <main id="main">')
    if "<header" not in body or "<nav" not in body:
        bad.append("the body needs a <header> containing a <nav>")
    if len(body.encode()) > MAX_PAGE_BYTES:
        bad.append("the page is too large (keep it under 40 KB)")
    p = _Body()
    try:
        p.feed(body)
        p.close()
    except Exception as exc:
        bad.append(f"the HTML could not be parsed: {type(exc).__name__}")
    seen, uniq = set(), []
    for x in p.problems:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return bad + uniq[:8]


def assemble(css: str, body: str, biz: dict, *, sender: str) -> str:
    name = build._h(biz.get("name"))
    banner = (f"{build.CONCEPT_NOTE} of {name}. Prepared by {build._h(sender)} for the owner to look over. "
              "Nothing here is final, and it is not affiliated with the business.")
    tel = build.tel_digits(biz.get("phone") or "")
    callbar = (f'<div class="cc-callbar"><a class="cc-btn" href="tel:{build._h(tel)}">Call {build._h(biz.get("phone"))}</a></div>' if tel else "")
    ours = """
.cc-skip{position:absolute;left:-9999px}.cc-skip:focus{left:1rem;top:1rem;background:var(--surface);color:var(--text);padding:.6rem 1rem;z-index:99}
.cc-banner{background:var(--surface);color:var(--text);padding:.6rem 1rem;text-align:center;font:.9rem/1.45 system-ui,sans-serif;border-bottom:3px solid var(--accent)}
.cc-foot{background:var(--bg);color:var(--text);padding:2rem 1.25rem 1.5rem;font:.95rem/1.55 system-ui,sans-serif;border-top:1px solid var(--muted)}
.cc-foot p{margin:0 auto .7rem;max-width:62rem}.cc-foot strong{font-weight:800}
.cc-callbar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:none;padding:.6rem .8rem;background:var(--bg);border-top:1px solid var(--muted)}
.cc-btn{display:flex;align-items:center;justify-content:center;min-height:2.9rem;border-radius:.5rem;background:var(--primary);color:var(--on-primary);font:800 1.05rem system-ui,sans-serif;text-decoration:none}
@media(max-width:760px){.cc-callbar{display:block}body{padding-bottom:4.5rem}}
.cc-skip:focus-visible,.cc-btn:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{name} | concept preview</title>
<style>
{css}
{ours}
</style>
</head>
<body>
<a class="cc-skip" href="#main">Skip to content</a>
<div class="cc-banner" role="note">{banner}</div>
{body}
<footer class="cc-foot"><p><strong>Contact form disabled.</strong> This concept does not collect or send messages. A working form or booking tool would be set up if the owner approves the site.</p><p>{banner}</p><p>To have this preview removed, reply to the email it came with and it will be taken down.</p></footer>
{callbar}
</body>
</html>
"""


Generate = Callable[[str], Optional[str]]


def design(biz: dict, facts: dict, *, sender: str, generate: Generate, tries: int = TRIES,
           extra_checks: Optional[Callable[[str, dict, dict], list]] = None) -> tuple[Optional["build.Site"], list[str]]:
    """(site, notes). site is None when no attempt passed; notes say what went wrong, for the record.

    `generate(prompt) -> text or None`. `extra_checks(html, tokens, used) -> [problems]` lets the caller add the
    static checks (kept out of here so this module does not depend on how the pipeline records results)."""
    direction = seed_direction(biz.get("name") or "")
    problems: list[str] = []
    notes: list[str] = []
    shown = {"services": [s for s in (facts.get("services") or []) if s][:6], "hours": facts.get("hours") or ""}
    used = {"name": (biz.get("name") or "").strip(), "phone": biz.get("phone") or "", "email": biz.get("email") or "",
            "address": biz.get("address") or "", "city": biz.get("city") or "", "services": shown["services"],
            "hours": shown["hours"], "category": build.CATEGORY_TITLES.get(biz.get("category") or "", "Local business")}
    for attempt in range(1, tries + 1):
        raw = generate(prompt(biz, facts, direction=direction, problems=problems))
        if not raw:
            notes.append(f"attempt {attempt}: the model did not answer")
            break                                               # unavailable or out of quota: do not hammer it
        css, body = split_reply(raw)
        if not css or not body:
            problems = ["the reply must contain ===CSS=== then ===BODY=== exactly as described"]
        else:
            problems = css_problems(css) + body_problems(body)
        html = ""
        tokens = parse_tokens(css) if css else {}
        if not problems:
            html = assemble(css, body, biz, sender=sender)
            if extra_checks:
                problems = extra_checks(html, tokens, used)
        if not problems:
            site = build.Site(files={"index.html": html}, theme="ai", layout={"direction": direction}, used=used)
            site.tokens = tokens                                  # type: ignore[attr-defined]
            return site, notes
        notes.append(f"attempt {attempt}: " + "; ".join(problems[:4]))
    return None, notes


# ── the real model ───────────────────────────────────────────────────────────
def gemini_generate(timeout_s: float = 150.0) -> Optional[Generate]:
    """A `generate` that asks Gemini over REST, newest model first. None if there is no key or no SDK."""
    try:
        from core import gemini
        key = gemini.api_key()
        if not key:
            return None
        from google.genai import types
    except Exception:
        return None

    def generate(text: str) -> Optional[str]:
        try:
            cl = gemini.client(timeout_ms=int(timeout_s * 1000), key=key)
        except Exception:
            return None
        for model in MODEL_LADDER:
            try:
                resp = cl.models.generate_content(
                    model=model, contents=text,
                    config=types.GenerateContentConfig(temperature=0.9, max_output_tokens=MAX_TOKENS))
                out = (getattr(resp, "text", None) or "").strip()
                if out:
                    return out
            except Exception as exc:                            # not found, quota, overloaded: try the next model
                print(f"[design] {model}: {type(exc).__name__}: {str(exc)[:120]}")
        return None
    return generate
