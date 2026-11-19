"""The AI design workflow, minus persistence (services.py stores everything this returns).

1. interpret()      customer request -> structured brief (deterministic, so it is testable and never invents facts)
2. concept_prompt() brief + one creative direction -> the image prompt (our signature style)
3. Provider.render() prompt -> front + back concept images (demo | codex | openai)
4. run_checks()     images vs requested colours, garment, lettering, personalisation
5. revise_brief()   base brief + customer note -> new brief that keeps the chosen design's important features
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIGNATURE_STYLE = (
    "Signature style: vintage early-2000s sportswear. Flowing asymmetric waves and warped stripes, washed colours, "
    "distressed varsity lettering with a printed crackle and faded-ink texture, oversized graphics with names and "
    "numbers that stay readable, coordinated front-and-back artwork that lines up at the side seams, aged cream and "
    "muted silver accents, realistic matte fabric. One dominant base colour, one or two supporting colours, one "
    "restrained accent.")
HARD_RULES = (
    "Hard rules: no official team logos, crests, wordmarks, league marks or sponsor marks; no real player likeness; "
    "no badge that resembles a real club's; nothing that suggests the design is official or licensed. Spell every "
    "word exactly as given. No skeletons or skulls unless the brief explicitly asks for them.")

GARMENT_PHRASE = {
    "soccer": "short-sleeve collared soccer jersey",
    "basketball": "sleeveless basketball jersey",
    "tee": "short-sleeve crew-neck shirt",
}

DIRECTIONS = {
    "vintage_waves": [
        ("Waves", "flowing asymmetric waves sweeping diagonally from the hip to the opposite shoulder"),
        ("Warp", "warped vertical stripes that bend through the torso, fading from washed base colour to aged cream"),
    ],
    "bold_graphic": [
        ("Emblem", "one oversized original emblem behind the chest number, cracked print texture"),
        ("Split", "a split-body colour block with a giant original motif crossing the centre seam"),
    ],
    "clean_athletic": [
        ("Pinstripe", "crisp tonal pinstripes with a contrast chest band and clean block numbers"),
        ("Panel", "contrast side panels with one sweeping accent stripe and a minimal chest number"),
    ],
}

# Colour words customers use -> RGB, for the colour check. Not exhaustive; unknown words are reported, not guessed.
COLOR_WORDS = {
    "black": (20, 20, 22), "white": (240, 240, 236), "cream": (233, 218, 184), "ivory": (236, 228, 205),
    "silver": (178, 182, 188), "grey": (128, 128, 128), "gray": (128, 128, 128), "charcoal": (54, 56, 60),
    "red": (196, 30, 40), "garnet": (128, 22, 44), "maroon": (110, 20, 35), "crimson": (170, 20, 45),
    "burgundy": (100, 18, 40), "pink": (232, 110, 160), "orange": (232, 112, 30), "gold": (205, 160, 50),
    "yellow": (240, 205, 40), "green": (30, 120, 60), "lime": (150, 210, 50), "teal": (20, 128, 128),
    "aqua": (60, 200, 200), "turquoise": (60, 190, 190), "blue": (30, 70, 170), "royal": (40, 60, 170),
    "navy": (22, 32, 72), "sky": (120, 175, 225), "purple": (90, 40, 140), "violet": (120, 70, 170),
    "brown": (110, 70, 40), "tan": (200, 170, 120), "beige": (220, 200, 160), "bronze": (160, 110, 60),
}

# Public colour associations used only as a starting palette when the customer leaves colours blank.
# These are colours, not marks; the hard rules keep logos and wordmarks out of every prompt.
KNOWN_SIDES = {
    "barcelona": ("FC Barcelona", ["garnet", "blue", "gold"]), "barca": ("FC Barcelona", ["garnet", "blue", "gold"]),
    "cowboys": ("Dallas Cowboys", ["navy", "silver", "white"]),
    "real madrid": ("Real Madrid", ["white", "gold", "purple"]),
    "raiders": ("Las Vegas Raiders", ["black", "silver"]),
    "lakers": ("Los Angeles Lakers", ["purple", "gold"]),
    "kobe": ("Kobe Bryant", ["purple", "gold"]),
    "ronaldinho": ("Ronaldinho", ["yellow", "green", "blue"]),
    "giannis": ("Giannis Antetokounmpo", ["green", "cream", "blue"]),
    "antetokounmpo": ("Giannis Antetokounmpo", ["green", "cream", "blue"]),
    "anthony edwards": ("Anthony Edwards", ["navy", "green", "blue"]),
    "edwards": ("Anthony Edwards", ["navy", "green", "blue"]),
    "bucks": ("Milwaukee Bucks", ["green", "cream"]),
    "timberwolves": ("Minnesota Timberwolves", ["navy", "green"]),
    "bulls": ("Chicago Bulls", ["red", "black"]),
    "celtics": ("Boston Celtics", ["green", "white"]),
    "warriors": ("Golden State Warriors", ["blue", "gold"]),
    "manchester united": ("Manchester United", ["red", "black", "white"]),
    "man utd": ("Manchester United", ["red", "black", "white"]),
    "liverpool": ("Liverpool FC", ["red", "white"]),
    "arsenal": ("Arsenal", ["red", "white"]),
    "chelsea": ("Chelsea FC", ["blue", "white"]),
    "juventus": ("Juventus", ["black", "white"]),
    "ac milan": ("AC Milan", ["red", "black"]),
    "inter miami": ("Inter Miami", ["pink", "black"]),
    "psg": ("Paris Saint-Germain", ["navy", "red"]),
    "messi": ("Lionel Messi", ["sky", "white"]),
    "ronaldo": ("Cristiano Ronaldo", ["red", "white"]),
    "lebron": ("LeBron James", ["purple", "gold"]),
    "jordan": ("Michael Jordan", ["red", "black"]),
    "brazil": ("Brazil national team", ["yellow", "green", "blue"]),
    "argentina": ("Argentina national team", ["sky", "white"]),
    "yankees": ("New York Yankees", ["navy", "white"]),
    "dodgers": ("Los Angeles Dodgers", ["blue", "white"]),
    "eagles": ("Philadelphia Eagles", ["green", "silver"]),
    "chiefs": ("Kansas City Chiefs", ["red", "gold"]),
}
SPLIT = re.compile(r"\s*(?:×|✕|\bx\b|\bvs\.?\b|\bversus\b|\bmeets\b|\+|&|\band\b)\s*", re.I)


def parse_colors(text: str):
    words = re.findall(r"[a-z]+", (text or "").lower())
    known = [w for w in words if w in COLOR_WORDS]
    return list(dict.fromkeys(known)), [w for w in words if w not in COLOR_WORDS and w not in
                                        {"and", "with", "a", "of", "the", "accents", "accent", "light", "dark",
                                         "base", "some", "bit", "touch", "or", "colors", "colours", "main"}]


def find_known(text: str):
    t = (text or "").lower()
    hits = []
    for key, (name, pal) in KNOWN_SIDES.items():
        if re.search(r"\b" + re.escape(key) + r"\b", t) and name not in [h[0] for h in hits]:
            hits.append((name, pal))
    return hits


def interpret(req: dict, styles: dict, garments: dict) -> dict:
    """Structured brief from a request row. Pure function."""
    idea = req["idea"].strip()
    sides = [s.strip(" .,!") for s in SPLIT.split(idea) if s.strip(" .,!")]
    if len(sides) > 4:
        sides = [idea]
    known = find_known(idea)
    colors, unknown_color_words = parse_colors(req.get("colors", ""))
    palette_source = "customer"
    if not colors:
        palette_source = "inspiration" if known else "designer"
        colors = list(dict.fromkeys(c for _, pal in known for c in pal))[:4] or ["charcoal", "cream", "red", "blue"]
    third_party = bool(known) or (len(sides) >= 2 and not req.get("own_brand"))
    style = req["style"]
    front_text = ""
    if not third_party:
        front_text = sides[0].upper()[:24] if len(sides) == 1 and len(sides[0]) <= 24 else ""
    brief = {
        "idea": idea,
        "sides": sides,
        "recognised": [k[0] for k in known],
        "garment": req["garment"],
        "garment_label": garments.get(req["garment"], {}).get("label", req["garment"]),
        "garment_phrase": GARMENT_PHRASE.get(req["garment"], req["garment"]),
        "style": style,
        "style_label": styles.get(style, {}).get("label", style),
        "palette": colors,
        "palette_source": palette_source,
        "unrecognised_colour_words": unknown_color_words[:8],
        "front_text": front_text,
        "back_name": (req.get("back_name") or "").strip().upper(),
        "back_number": (req.get("back_number") or "").strip(),
        "customer_notes": (req.get("instructions") or "").strip(),
        "admin_notes": (req.get("admin_instructions") or "").strip(),
        "has_references": bool(req.get("_reference_count")),
        "third_party_branding": third_party,
        "branding_approach": ("Inspired by the colours and spirit of the named sides only. Original motifs, no official "
                              "marks, no team or player names on the garment unless permission is recorded.")
        if third_party else "Customer's own idea.",
        "directions": [{"key": k, "label": lbl, "layout": layout}
                       for k, (lbl, layout) in zip("AB", DIRECTIONS.get(style, DIRECTIONS["vintage_waves"]))],
        "preserve": [],
        "revision_notes": [],
    }
    if third_party and req.get("branding_status") == "cleared" and len(sides) >= 2:
        brief["front_text"] = " × ".join(s.upper() for s in sides[:2])[:30]
    return brief


def revise_brief(base: dict, direction_key: str, note: str) -> dict:
    """A new brief for one revised concept. Keeps the chosen concept's palette, layout and lettering unless the
    customer's note asks otherwise; the note is passed through verbatim."""
    b = json.loads(json.dumps(base))
    d = next((x for x in b["directions"] if x["key"] == direction_key), b["directions"][0])
    b["directions"] = [dict(d)]
    b["preserve"] = list(dict.fromkeys(base.get("preserve", []) + [
        f"layout: {d['layout']}", f"palette: {', '.join(base['palette'])}",
        "lettering style and number placement", "front-and-back coordination"]))
    b["revision_notes"] = base.get("revision_notes", []) + [note.strip()]
    return b


def concept_prompt(brief: dict, direction: dict, view: str) -> str:
    """Prompt for one photorealistic concept mockup (view = front | back)."""
    g = brief["garment_phrase"]
    lines = [
        f"Photorealistic product mockup of a {g}, {view} view, laid flat on a warm off-white studio background, soft "
        f"daylight, realistic matte fabric with natural folds. Tall portrait image.",
        SIGNATURE_STYLE,
        f"Style family: {brief['style_label']}. Concept {direction['key']}: {direction['layout']}.",
        f"Inspiration: {brief['idea']}. {brief['branding_approach']}",
        f"Palette: {', '.join(brief['palette'])}.",
    ]
    if view == "front":
        lines.append(f"Front lettering: \"{brief['front_text']}\" in big arched distressed varsity letters above the number."
                     if brief["front_text"] else "Front: no words; a large chest number only.")
        if brief["back_number"]:
            lines.append(f"Chest number: {brief['back_number']}.")
    else:
        if brief["back_name"]:
            lines.append(f"Back: the name \"{brief['back_name']}\" arched across the shoulders, spelled exactly.")
        lines.append(f"Back number: {brief['back_number'] or 'none'}, oversized, same lettering style as the front.")
        lines.append("Match the front's colours, waves and texture so the panels line up at the side seams.")
    if brief["preserve"]:
        lines.append("Keep from the chosen design: " + "; ".join(brief["preserve"]) + ".")
    if brief["revision_notes"]:
        lines.append("Customer's requested changes (apply these): " + " | ".join(brief["revision_notes"]))
    if brief["customer_notes"]:
        lines.append(f"Customer notes: {brief['customer_notes']}")
    if brief["admin_notes"]:
        lines.append(f"Designer notes: {brief['admin_notes']}")
    lines.append(HARD_RULES)
    return "\n".join(lines)


# ── Providers ───────────────────────────────────────────────────────────────────────────────────────────────────────

class ProviderError(RuntimeError):
    """`retryable` False means trying again won't help (missing credentials, refused prompt)."""

    def __init__(self, msg, retryable=True):
        super().__init__(msg)
        self.retryable = retryable


class DemoProvider:
    """Draws a clearly stamped placeholder. Never presented as generated from the customer's request."""
    name, is_demo = "demo", True

    def render(self, brief, direction, prompts):
        out = {}
        for view in ("front", "back"):
            out[view] = _demo_image(brief, direction, view)
        return out


class CodexProvider:
    """ChatGPT image generation through the Codex CLI signed in on this machine (no API key in this app)."""
    name, is_demo = "codex", False

    def __init__(self, timeout=900, run=subprocess.run):
        self.timeout, self.run = timeout, run

    def render(self, brief, direction, prompts):
        exe = shutil.which("codex") or _codex_fallback()
        if not exe:
            raise ProviderError("The Codex CLI isn't installed on the server.", retryable=False)
        task = ("Use your image generation tool to make two images and save them in the current directory as "
                "front.png and back.png (tall portrait, 1024x1536). Reply with just the file names.\n\n"
                f"front.png:\n{prompts['front']}\n\nback.png:\n{prompts['back']}")
        with tempfile.TemporaryDirectory(prefix="collab-") as tmp:
            env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
            try:
                r = self.run([exe, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "--ephemeral", "-"],
                             input=task, capture_output=True, text=True, cwd=tmp, env=env, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError("Image generation timed out.")
            out = {}
            for view in ("front", "back"):
                p = Path(tmp) / f"{view}.png"
                if not p.is_file():
                    raise ProviderError(f"The image tool didn't return a {view} image (exit {r.returncode}).")
                out[view] = p.read_bytes()
            return out


class OpenAIImagesProvider:
    """OpenAI Images API with a server-side key."""
    name, is_demo = "openai", False

    def __init__(self, api_key, model="gpt-image-1", post=None):
        self.api_key, self.model = api_key, model
        if post is None:
            import requests
            post = requests.post
        self.post = post

    def render(self, brief, direction, prompts):
        if not self.api_key:
            raise ProviderError("No image-generation API key is configured.", retryable=False)
        out = {}
        for view in ("front", "back"):
            r = self.post("https://api.openai.com/v1/images/generations", timeout=300,
                          headers={"Authorization": f"Bearer {self.api_key}"},
                          json={"model": self.model, "prompt": prompts[view], "size": "1024x1536", "n": 1})
            if r.status_code in (400, 401, 403):
                msg = (r.json().get("error", {}) or {}).get("message", "") if _is_json(r) else ""
                raise ProviderError(f"The image service refused the request ({r.status_code}). {msg[:200]}",
                                    retryable=False)
            if r.status_code >= 300:
                raise ProviderError(f"The image service returned {r.status_code}.")
            out[view] = base64.b64decode(r.json()["data"][0]["b64_json"])
        return out


def _is_json(r):
    try:
        r.json()
        return True
    except ValueError:
        return False


def _codex_fallback():
    import glob
    for pat in [str(Path.home() / ".vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex"),
                "/Applications/Codex.app/Contents/Resources/codex"]:
        for p in sorted(glob.glob(pat), reverse=True):
            if os.access(p, os.X_OK):
                return p
    return ""


def make_provider(env):
    if env.image_provider == "codex":
        return CodexProvider()
    if env.image_provider == "openai":
        return OpenAIImagesProvider(env.openai_api_key, env.openai_image_model)
    return DemoProvider()


# ── Checks ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def dominant_colors(data: bytes, k: int = 8):
    with Image.open(io.BytesIO(data)) as im:
        small = im.convert("RGB").resize((96, 144))
    q = small.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[: k * 3]
    counts = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    return [(tuple(pal[i * 3:i * 3 + 3]), c / total) for c, i in counts]


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def run_checks(brief: dict, images: dict, provider_name: str, is_demo: bool) -> dict:
    items = []

    def add(name, status, detail):
        items.append({"check": name, "status": status, "detail": detail})

    if is_demo:
        add("Demo mode", "demo", "Placeholder image from demo mode. It was not generated from this request and must "
            "be replaced (regenerate with a real provider or upload a concept) before it can be sent as a real design.")
    for view in ("front", "back"):
        data = images.get(view)
        if not data:
            add(f"{view.title()} image", "fail", "missing")
            continue
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        add(f"{view.title()} image", "pass" if min(w, h) >= 700 else "warn", f"{w}×{h}px")
    if not is_demo and images.get("front"):
        dom = dominant_colors(images["front"]) + (dominant_colors(images["back"]) if images.get("back") else [])
        for c in brief["palette"]:
            rgb = COLOR_WORDS.get(c)
            if not rgb:
                continue
            best = min(_dist(rgb, d) for d, share in dom if share > 0.02) if dom else 999
            add(f"Colour: {c}", "pass" if best < 70 else "warn",
                "present" if best < 70 else "not clearly present — check by eye")
    if brief["unrecognised_colour_words"]:
        add("Colour words", "review", "Couldn't check automatically: " + ", ".join(brief["unrecognised_colour_words"]))
    add("Garment type", "review", f"Prompted as a {brief['garment_phrase']}. Confirm the image shows that garment.")
    if brief["back_name"] or brief["back_number"]:
        add("Personalisation", "review", f"Confirm the back reads exactly “{brief['back_name']}” "
            f"and number “{brief['back_number']}”. Image models can misspell lettering.")
    if brief["front_text"]:
        add("Front lettering", "review", f"Confirm the front reads exactly “{brief['front_text']}”.")
    if brief["third_party_branding"]:
        add("Branding", "review", "Request names real teams or players: confirm no official logos, crests or "
            "wordmarks appear.")
    worst = "pass"
    for rank in ("demo", "fail", "warn", "review"):
        if any(i["status"] == rank for i in items):
            worst = rank
            break
    return {"overall": worst, "items": items, "provider": provider_name}


# ── Demo art ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _font(size):
    for p in ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _demo_image(brief, direction, view) -> bytes:
    W, H = 800, 1100
    pal = [COLOR_WORDS.get(c, (90, 90, 90)) for c in brief["palette"]] or [(54, 56, 60), (233, 218, 184)]
    base = pal[0] if direction["key"] == "A" or len(pal) < 2 else pal[1]
    support = pal[1] if base == pal[0] and len(pal) > 1 else pal[0]
    im = Image.new("RGB", (W, H), (236, 232, 222))
    mask = Image.new("L", (W, H), 0)
    m = ImageDraw.Draw(mask)
    if brief["garment"] == "basketball":
        body = [(230, 120), (330, 120), (360, 190), (440, 190), (470, 120), (570, 120), (600, 330), (620, 1000),
                (180, 1000), (200, 330)]
    else:
        body = [(250, 110), (330, 110), (400, 160), (470, 110), (550, 110), (720, 230), (650, 400), (600, 360),
                (610, 1000), (190, 1000), (200, 360), (150, 400), (80, 230)]
    m.polygon(body, fill=255)
    art = Image.new("RGB", (W, H), base)
    d = ImageDraw.Draw(art)
    for i in range(-6, 14):
        y = i * 90 + (40 if view == "back" else 0)
        d.line([(0, y + 300), (W, y - 120)], fill=support, width=34 if i % 3 == 0 else 10)
    art = art.filter(ImageFilter.GaussianBlur(1.2))
    im.paste(art, (0, 0), mask)
    d = ImageDraw.Draw(im)
    cream = (233, 218, 184)
    num = brief["back_number"] or "00"
    if view == "back" and brief["back_name"]:
        f = _font(64)
        tw = d.textlength(brief["back_name"], font=f)
        d.text(((W - tw) / 2, 300), brief["back_name"], font=f, fill=cream, stroke_width=3, stroke_fill=(30, 30, 30))
    f = _font(260)
    tw = d.textlength(num, font=f)
    d.text(((W - tw) / 2, 420), num, font=f, fill=cream, stroke_width=6, stroke_fill=(30, 30, 30))
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    b = ImageDraw.Draw(band)
    b.rectangle([0, 1010, W, 1100], fill=(20, 20, 22, 235))
    b.text((24, 1022), f"DEMO PLACEHOLDER · concept {direction['key']} · {view}", font=_font(30), fill=(240, 240, 236))
    b.text((24, 1062), "Not generated from this request", font=_font(22), fill=(230, 120, 110))
    im = Image.alpha_composite(im.convert("RGBA"), band).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
