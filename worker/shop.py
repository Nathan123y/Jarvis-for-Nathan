"""The Etsy print-on-demand shop, run by Jarvis from the background worker.

Once a day (and on request) it:
  1. invents one design for a gift niche (Gemini, free): an illustration prompt, a short slogan, a title,
     a description and 13 Etsy tags. Brands, characters, celebrities, teams and lyrics are refused twice:
     in the prompt and by BLOCKED below, because copyright complaints are how print-on-demand shops get closed;
  2. draws it (Cloudflare Workers AI, flux-1-schnell, free tier), cuts away the white background and sets the
     slogan under it at print size;
  3. puts it on a T-shirt, a mug and a sticker in Printify, priced so each sale clears `profit` after
     Printify's cost and Etsy's fees, and publishes them to the Etsy shop connected in Printify.

Printify prints and ships each order; Etsy pays out; Printify charges its cost to the card in Printify.
Nothing is published until an Etsy store is connected in Printify: designs wait in the queue meanwhile.
Every listing says the design was made with AI and printed on demand, as Etsy asks.

State: memory/etsy_shop.json (designs, products, orders seen); artwork: memory/etsy_shop/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "memory" / "etsy_shop.json"
ART = ROOT / "memory" / "etsy_shop"
NS = "etsy_shop"
API = "https://api.printify.com/v1/"
FLUX = "@cf/black-forest-labs/flux-1-schnell"
FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
DESIGN_W = 3000                      # the artwork is set this wide: a shirt's print area is 2767 px

PRODUCTS = {
    "tee": {"name": "T-Shirt", "blueprint": 12, "provider": 99, "profit": 8.0,      # Bella+Canvas 3001
            "colors": ("White", "Natural", "Ash", "Athletic Heather", "Soft Cream", "Heather Peach"),
            "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "hoodie": {"name": "Hoodie", "blueprint": 77, "provider": 99, "profit": 10.0,  # Gildan 18500
               "colors": ("White", "Sport Grey", "Ash", "Sand", "Light Pink", "Light Blue"),
               "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.95},
    "tank": {"name": "Tank Top", "blueprint": 39, "provider": 99, "profit": 7.0,    # Bella+Canvas 3480
             "colors": ("White", "Athletic Heather"), "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "shorts": {"name": "Sweat Shorts", "blueprint": 1574, "provider": 39, "profit": 7.0,   # Comfort Colors, printed on the left leg
               "position": "front_left_leg", "colors": ("Ivory", "Peachy", "Hydrangea", "Blue Jean"),
               "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.85},
    "pants": {"name": "Sweatpants", "blueprint": 1588, "provider": 217, "profit": 7.0,     # Comfort Colors, the left thigh
              "position": "left_leg_front", "colors": ("Ivory", "Peachy", "Hydrangea", "Blue Jean"),
              "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.95, "y": 0.3},
    "hat": {"name": "Trucker Hat", "blueprint": 1128, "provider": 41, "profit": 7.0,       # printed (not embroidered) front panel
            "art": "slogan", "colors": ("Black", "Dark Navy", "Dark Green", "Royal"), "sizes": ("One size",), "fill": 0.95},
    "mug": {"name": "Mug", "blueprint": 478, "provider": 99, "profit": 6.0, "sizes": ("11oz",), "fill": 0.9},
    "sticker": {"name": "Sticker", "blueprint": 400, "provider": 99, "profit": 2.5, "fill": 1.0},   # no longer made (2026-10-24)
    # added 2026-10-26
    "crewneck": {"name": "Crewneck Sweatshirt", "blueprint": 49, "provider": 99, "profit": 10.0,   # Gildan 18000
                 "colors": ("White", "Sport Grey", "Ash", "Sand", "Light Pink", "Light Blue"),
                 "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "poster": {"name": "Poster", "blueprint": 282, "provider": 99, "profit": 8.0,             # matte, vertical
               "sizes": ("11\u2033 x 14\u2033", "12\u2033 x 18\u2033", "16\u2033 x 20\u2033", "18\u2033 x 24\u2033"), "fill": 0.85},
    "phone": {"name": "Phone Case", "blueprint": 421, "provider": 99, "profit": 7.0,          # Tough Case, glossy
              "sizes": ("iPhone 13", "iPhone 14", "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 16",
                        "iPhone 16 Pro", "iPhone 16 Pro Max", "iPhone 17"), "fill": 0.8},
    "tote": {"name": "Tote Bag", "blueprint": 1313, "provider": 99, "profit": 6.0, "fill": 0.85},  # Liberty Bags canvas
    "ornament": {"name": "Christmas Ornament", "blueprint": 1599, "provider": 99, "profit": 6.0,    # ceramic, 4 shapes
                 "fill": 0.92},
    "tumbler": {"name": "Tumbler", "blueprint": 353, "provider": 1, "profit": 7.0, "fill": 0.8},   # 20oz
    # the ugly-sweater line (2026-10-26, from the user's "GET LIT" photo): its art is light on dark, so dark garments
    "sweater_tee": {"name": "T-Shirt", "blueprint": 12, "provider": 99, "profit": 8.0,
                    "colors": ("Black", "Canvas Red", "Forest"), "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "sweater_hoodie": {"name": "Hoodie", "blueprint": 77, "provider": 99, "profit": 10.0,
                       "colors": ("Black", "Red", "Navy", "Forest Green"), "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    # printed all over (sleeves, cuffs, waistband); Printify photographs it on a person, unlike the knit sweater
    # (blueprint 1748), whose only photo is the flat printing template
    "ugly_sweater": {"name": "Ugly Christmas Sweater", "blueprint": 1023, "provider": 90, "profit": 12.0,
                     "art": "sweater", "sizes": ("S", "M", "L", "XL", "2XL")},
}
SWEATER_PRODUCTS = ["ugly_sweater", "sweater_tee", "sweater_hoodie"]
SWEATER_NOTE = """This one is an UGLY CHRISTMAS SWEATER design: the picture is always a Christmas tree with glowing
lights (drawn for you), so only the words matter. The slogan is a short, punchy pun of 2-4 words (never a sentence) that plays on
lights, trees, being lit, ornaments, tinsel, Santa or the season, in the spirit of "GET LIT" but your own. Put
"ugly christmas sweater" in the title and tags; image_prompt can just be "christmas tree"."""
# Holiday lines: while a season runs, one extra design a day is made for it, on the products that sell for it.
# (start, end) are (month, day); the end leaves time to ship before the holiday. Two lines running at once
# take turns, a day each. Halloween is left out this
# year: added on Oct 26, too late to arrive by the 31st.
SEASONS = {
    "christmas": {"start": (10, 26), "end": (12, 14),
                  "products": ["tee", "crewneck", "hoodie", "mug", "ornament", "tumbler", "poster", "tote"],
                  "note": "a Christmas / holiday-season gift (ugly-sweater energy, holiday stress, family chaos, "
                          "Santa, elves, reindeer, snowmen, gingerbread, gift-giving, winter break). Aim most designs "
                          "at what people search for in December: a white elephant / Secret Santa gag gift, a gift for "
                          "a coworker or boss, an ugly Christmas sweater party, a matching family or friend-group "
                          "shirt; say which one in the title and tags"},
    "thanksgiving": {"start": (11, 1), "end": (11, 18),
                     "products": ["tee", "crewneck", "hoodie", "mug", "tote"],
                     "note": "Thanksgiving (turkey, pie, food coma, family dinner chaos, Friendsgiving, being thankful "
                             "ironically)"},
    "halloween": {"start": (9, 15), "end": (10, 20),
                  "products": ["tee", "crewneck", "hoodie", "mug", "tote"],
                  "note": "Halloween (spooky season, ghosts, skeletons, pumpkins, witches, costumes, candy)"},
}
SEASON_NOTE = """This design is for the shop's {name} line: make it about {note}. Same voice: funny, edgy,
meme-literate and giftable, not a generic greeting card. Put the holiday in the title and tags (e.g. "funny
christmas shirt", "christmas gift for coworker", "ugly christmas sweater")."""
# Sales channels connected in Printify that designs are listed on. eBay titles stop at 80 characters. The
# Printify Pop-Up Store charges nothing to list, so it gets every design and comes first on the site's buy links.
CHANNELS = {"popup": {"name": "our shop", "title_max": 140}, "etsy": {"name": "Etsy", "title_max": 140},
            "ebay": {"name": "eBay", "title_max": 80}}
# Printify renders dozens of product photos but sends Etsy one or two, and only says what they are when the
# product is created; these are saved then, for the listing (once Jarvis can reach Etsy) and the storefront.
# Left out: the folded shirt (it hides the design) and the mug scenes (they look at the side of the mug,
# where a centred design wraps out of sight). Every listing also gets a close-up of the design itself.
EXTRA_PHOTOS = {"tee": ("person-1", "person-2", "lifestyle", "hanging-1", "duo"),
                "hoodie": ("person-1", "person-2", "lifestyle", "duo"), "tank": ("person-1", "person-2", "lifestyle"),
                "mug": (),
                "sticker": ("context-1", "context-2")}
MAX_MOCKUPS = 7                     # kinds not listed above get every other angle, up to this many
DEFAULTS = {"enabled": True, "designs_per_day": 1, "time": "06:00",  # live before the 7 AM text
            "products": ["tee", "hoodie", "tank", "shorts", "pants", "hat", "mug",    # the user's lineup (2026-10-24): no stickers
                         "crewneck", "poster", "phone", "tote"],                    # added 2026-10-26
            "holiday_per_day": 1,                # extra designs a day for the running holiday line (0 = none)
            # Etsy charges $0.20 a listing (2026-10-27, user: keep it to ~$0.80 a day): one holiday design a day,
            # on the products that sell for the holidays. 0 a day = nothing new on Etsy.
            "etsy_per_day": 1, "etsy_holiday_only": True,
            "etsy_products": ["ugly_sweater", "sweater_tee", "sweater_hoodie", "tee", "hoodie", "mug"],
            # eBay lists 250 a month free, and its listings renew monthly against that, so live listings stay under it
            "ebay_max": 250,
            "video_times": ""}                 # e.g. "11:00,15:00,19:00", one video at each; empty = no videos (off: user prefers the photos)

BLOCKED = re.compile(r"(?<![a-z])(" + "|".join([
    "disney", "pixar", "marvel", "dc comics", "batman", "superman", "spider-?man", "avengers", "star wars", "jedi",
    "harry potter", "hogwarts", "pokemon", "pikachu", "nintendo", "mario", "zelda", "sonic", "minecraft", "fortnite",
    "barbie", "hello kitty", "sanrio", "snoopy", "peanuts", "looney", "mickey", "minnie", "simpsons", "sesame",
    "nike", "adidas", "gucci", "chanel", "louis vuitton", "supreme", "coca-?cola", "pepsi", "starbucks", "mcdonald",
    "nfl", "nba", "mlb", "nhl", "fifa", "ncaa", "olympic", "taylor swift", "swiftie", "beyonce", "elvis", "beatles",
    "friends tv", "the office", "stranger things", "game of thrones", "lord of the rings", "hobbit", "grinch",
    "dr\\.? seuss", "winnie", "bluey", "paw patrol", "peppa", "baby shark", "yoda", "grogu", "trump", "biden",
    "rudolph", "frosty", "elf on the shelf", "charlie brown", "home alone", "christmas story", "lampoon",
    "griswold", "mariah", "polar express", "nightmare before christmas", "jack skellington", "hocus pocus",
    "harley", "jeep", "jordans?", "air jordan", "skibidi", "windows xp", "microsoft", "apple", "iphone", "google", "chatgpt", "openai", "tiktok", "instagram", "netflix", "amazon", "ford", "chevy", "john deere", "yeti", "stanley cup", "lego", "crocs",
]) + r")(?![a-z])", re.I)

IDEA_PROMPT = """You design products for an Etsy print-on-demand shop that sells T-shirts, mugs and stickers to
people aged roughly 16-35 who live online. The shop's voice is edgy, sarcastic and meme-literate: the kind of
shirt that makes a stranger laugh, take a photo or ask where you got it. Not family friendly, not wholesome,
not "Live Laugh Love". Invent ONE new design.

Good directions: internet and meme humour (original jokes in the style of current memes and slang), unhinged or
chaotic energy, brutal honesty about work, school, money, dating, sleep, anxiety, gym, gaming and adulting,
dark-but-harmless humour, absurd deadpan statements, ironic fake motivation, cursed-looking cute animals.
Swearing is fine, uncensored (shit, damn, f*ck, hell, ass, etc.) when it makes the joke land; crude is fine
too, but it still has to be funny and quotable.

{trends}

{notes}

Rules, all required:
- Never: slurs, hate or jokes punching down at a group, sexual acts or nudity, drugs, violence against
  real people, self-harm encouragement, politics or religion.
- Never name or depict a real person (celebrities, streamers, athletes, politicians, people from viral
  clips), and never use song lyrics, song titles or artist names: those get listings taken down.
- No brands, trademarks, logos, characters, celebrities, sports teams, movies, TV, games, songs, quotes or
  slogans someone else owns, and no famous meme images or meme characters (Pepe, Doge, This Is Fine dog,
  Wojak, etc.) - the joke and the drawing must be original.
- The illustration: one funny, weird or deadpan character or object (e.g. a raccoon in a hoodie, a burnt-out
  coffee cup, a smug cat, a skeleton at a laptop), bold streetwear-style vector art, thick dark outlines,
  a few punchy colours, centred, isolated on a plain pure white background, no text, letters or numbers.
- The slogan is printed under the picture by us: 2 to 7 words (never more), ALL CAPS, plain ASCII, punchy and quotable.
- Title: what buyers type into Etsy search (e.g. "funny sarcastic shirt", "meme shirt", "gen z humor"),
  60-120 characters, no product type (we add "T-Shirt" etc.).
- Exactly 13 tags, each 20 characters or fewer, lowercase, what buyers search for.
- Don't repeat the theme or joke of these recent designs; pick a different topic: {recent}
- "niche" names the meme or trend you riffed on, or the topic if none fit.

Reply with JSON only:
{{"niche": "...", "image_prompt": "...", "slogan": "...", "title": "...", "description": "2-3 sentences in the shop's voice for the listing", "tags": ["...", ...]}}"""

TRENDS_NOTE = """What the internet is talking about TODAY (from Know Your Meme and Reddit):
{}

Pick the funniest meme, catchphrase, slang term or running joke from that list that works on a shirt (skip
news, sports and anything about a real person) and riff on it: use its phrase or format so people who are
online get it instantly, but write your own joke and draw your own original picture. If nothing there works,
use a meme or slang phrase that's been big online in the last few months instead."""


# ── small pieces ─────────────────────────────────────────────────────────────
# Printify's catalog answers "Too Many Attempts" when every pass asks again; a product's variants don't change
_VARIANTS: dict = {}
_AREAS: dict = {}                    # kind -> {print position: (w, h)}, for the all-over-print sweater


def season_for(day) -> Optional[str]:
    """The holiday line a day's extra design is for, or None outside every season."""
    md = (day.month, day.day)
    running = [n for n, x in SEASONS.items() if x["start"] <= md <= x["end"]]
    return running[day.toordinal() % len(running)] if running else None


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v not in (None, "")})
    try:
        out["designs_per_day"] = max(0, min(5, int(out["designs_per_day"])))
    except (TypeError, ValueError):
        out["designs_per_day"] = DEFAULTS["designs_per_day"]
    try:
        out["holiday_per_day"] = max(0, min(3, int(out["holiday_per_day"])))
    except (TypeError, ValueError):
        out["holiday_per_day"] = DEFAULTS["holiday_per_day"]
    for key in ("etsy_per_day", "ebay_max"):
        try:
            out[key] = max(0, int(out[key]))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    return out


def load_state(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("designs", [])
    st.setdefault("orders_seen", [])
    return st


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def overnight(since: float, path: Path = STATE) -> dict:
    """For the morning text: sales since `since` and the designs made since then."""
    st = load_state(path)
    sales = [x for x in st.get("sales_log") or [] if x.get("at", 0) >= since]
    return {"sales": len(sales), "revenue": round(sum(x.get("price") or 0 for x in sales), 2),
            "new": [d.get("slogan") or d.get("title", "") for d in st["designs"] if d.get("made", 0) >= since
                    and any(p.get("published") for p in d.get("products", {}).values())],
            "listed": sum(1 for d in st["designs"] if any(p.get("published") for p in d.get("products", {}).values()))}


def price_for(cost_cents: int, profit: float) -> int:
    """Cents, ending in .99, so `profit` is left after Printify's cost and Etsy's ~15% (fees, payment, ads)."""
    dollars = (cost_cents / 100 + profit) / 0.85
    return int(math.ceil(dollars)) * 100 - 1


def clean_idea(idea: dict) -> dict:
    """Etsy's limits (140-character title, 13 tags of 20 characters), and refuse anything off-limits."""
    if not isinstance(idea, dict) or not idea.get("image_prompt") or not idea.get("title"):
        raise ValueError("the idea came back incomplete")
    text = " ".join(str(idea.get(k, "")) for k in ("niche", "image_prompt", "slogan", "title", "description")) + \
        " " + " ".join(map(str, idea.get("tags") or []))
    hit = BLOCKED.search(text)
    if hit:
        raise ValueError(f"the idea used a protected name ({hit.group(0)})")
    tags, seen = [], set()
    for t in idea.get("tags") or []:
        t = re.sub(r"[^a-z0-9 '&-]", "", str(t).lower()).strip()[:20].strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    slogan = re.sub(r"[^A-Z0-9 '!?&.,*-]", "", str(idea.get("slogan") or "").upper()).strip()
    if len(slogan.split()) > 8:
        raise ValueError("the slogan is too long to print")
    return {"niche": str(idea.get("niche") or "")[:80], "image_prompt": str(idea["image_prompt"])[:900],
            "slogan": " ".join(slogan.split()), "title": " ".join(str(idea["title"]).split())[:110],
            "description": " ".join(str(idea.get("description") or "").split())[:600], "tags": tags[:13]}


# ── artwork ──────────────────────────────────────────────────────────────────
def cut_background(img, tolerance: int = 38):
    """The white around the illustration becomes transparent (flood-filled from the edges, so white
    inside the drawing stays)."""
    from PIL import Image, ImageDraw
    img = img.convert("RGBA")
    w, h = img.size
    marker = (255, 0, 255, 0)
    for x, y in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        r, g, b, a = img.getpixel((x, y))
        if r > 225 and g > 225 and b > 225:
            ImageDraw.floodfill(img, (x, y), marker, thresh=tolerance)
    px = img.load()
    for yy in range(h):
        for xx in range(w):
            if px[xx, yy] == marker:
                px[xx, yy] = (255, 255, 255, 0)
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def compose(art, slogan: str, width: int = DESIGN_W):
    """The illustration enlarged to `width`, the slogan set under it; a transparent PNG for printing."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    art = art.resize((width, round(art.height * width / art.width)), Image.LANCZOS).filter(
        ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    if not slogan:
        return art
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    size = 420
    while size > 120:
        font = ImageFont.truetype(FONT, size)
        lines = _wrap(slogan, font, draw, width)
        if len(lines) <= 2:
            break
        size -= 30
    line_h = round(size * 1.15)
    canvas = Image.new("RGBA", (width, art.height + 80 + line_h * len(lines)), (0, 0, 0, 0))
    canvas.paste(art, (0, 0), art)
    d = ImageDraw.Draw(canvas)
    y = art.height + 80
    for line in lines:
        w = d.textlength(line, font=font)
        d.text(((width - w) / 2, y), line, font=font, fill=(34, 30, 38, 255))
        y += line_h
    return canvas


def slogan_art(slogan: str, size: tuple = (3300, 1500)):
    """The slogan alone, for a trucker hat's wide front panel (white foam on every colour): bold dark letters."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = size
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pt = 520
    while pt > 100:
        font = ImageFont.truetype(FONT, pt)
        lines = _wrap(slogan, font, draw, w * 0.94)
        if len(lines) <= 3 and len(lines) * pt * 1.15 <= h * 0.92:
            break
        pt -= 20
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    line_h = round(pt * 1.15)
    y = (h - line_h * len(lines)) / 2
    for line in lines:
        tw = d.textlength(line, font=font)
        d.text(((w - tw) / 2, y), line, font=font, fill=(34, 30, 38, 255))
        y += line_h
    return canvas


def _wrap(text: str, font, draw, width: int) -> list:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width * 0.96 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def design_closeup(design: Path, dest: Path, size: int = 2000, detail: bool = False) -> None:
    """The design on a soft square backdrop: a clear look at the artwork for the listing photos.
    `detail` zooms in on the middle of the illustration, from the original drawing."""
    from PIL import Image
    art = Image.open(design).convert("RGBA")
    raw = design.with_name(design.stem + "-raw.jpg")
    if detail and raw.exists():
        art = cut_background(Image.open(raw))                    # the drawing alone, no slogan
        w, h = art.size                                          # then the middle 70 %, filling the frame
        art = art.crop((int(w * 0.15), int(h * 0.12), int(w * 0.85), int(h * 0.82)))
        art = art.resize((size, round(art.height * size / art.width)), Image.LANCZOS)
        backdrop = (226, 236, 228, 255)
    else:
        art.thumbnail((int(size * 0.8), int(size * 0.8)), Image.LANCZOS)
        backdrop = (244, 239, 230, 255)
    canvas = Image.new("RGBA", (size, size), backdrop)
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    canvas.convert("RGB").save(dest, "JPEG", quality=90)


def png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


ORDER_STATUS = {
    "sending-to-production": "is being sent to the printer",
    "in-production": "is being printed",
    "fulfilled": "has shipped",
    "partially-fulfilled": "has partly shipped",
    "canceled": "was canceled",
    "has-issues": "has a problem: check it in Printify",
    "on-hold": "is on hold in Printify (check your payment card)",
}


def order_update(order: dict, items: str, channel: str) -> Optional[str]:
    """The text for an order that changed status, with tracking once it ships."""
    what = ORDER_STATUS.get(str(order.get("status")))
    if not what:
        return None
    line = f"📦 Your {channel} order ({items}) {what}"
    track = next((sh for sh in order.get("shipments") or [] if sh.get("url") or sh.get("number")), None)
    if track and order.get("status") in ("fulfilled", "partially-fulfilled"):
        line += f": {track.get('carrier', '').upper()} {track.get('url') or track.get('number')}"
    return line + "."


GARMENT_WORDS = re.compile(r"\b(?:t-?shirts?|tees?|shirts?|tops?)\b", re.I)


def sweater_title(title: str) -> str:
    """The sweater line's search words up front ("Ugly Christmas Sweater" is what people type), in title case."""
    rest = re.sub(r"\bugly\s+christmas\s+sweat(?:er|\s*shirt)s?\b|\bsweat\s*shirts?\b|\bsweaters?\b", " ", title, flags=re.I)
    words = [w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:] for w in rest.split()]
    return " ".join(["Ugly Christmas Sweater"] + words)[:110]


def listing_title(base: str, product: str, limit: int) -> str:
    """'<title> <product>' within the channel's limit, cut at a word so the product name always fits. The idea's
    title often says "Shirt" or "Tee" anyway: dropped, so a mug isn't sold as a "Christmas Shirt ... Mug"."""
    base = re.sub(rf"\b{re.escape(product)}\b", " ", GARMENT_WORDS.sub(" ", base), flags=re.I)   # said once, at the end
    base = " ".join(base.split()).strip(" ,-|&")
    room = limit - len(product) - 1
    if len(base) > room:
        base = base[:room].rsplit(" ", 1)[0].rstrip(" ,-|&")
    return f"{base} {product}"


def products_in(rec: dict, channel: str) -> dict:
    """A design's products in one channel. Etsy's live in rec["products"] (the shop started there)."""
    if channel == "etsy":
        return rec.setdefault("products", {})
    return rec.setdefault("channels", {}).setdefault(channel, {})


def placements(kind: str, image_id: str, size: tuple, area: tuple) -> list:
    """Where the design goes in the print area. Printify's scale is relative to the area's width.
    Centred, even on a mug: the photo Printify sends to Etsy looks straight at the middle of the wrap,
    and a copy on each side of the handle (tried 2026-10-24) puts a seam right there."""
    (w, h), (pw, ph) = size, area
    fill = PRODUCTS[kind]["fill"]
    return [{"id": image_id, "x": 0.5, "y": PRODUCTS[kind].get("y", 0.5),
             "scale": round(min(1.0, (ph / pw) * (w / h)) * fill, 3), "angle": 0}]


# ── the services ─────────────────────────────────────────────────────────────
class Clients:
    """Gemini, Cloudflare and Printify, each replaceable in tests."""

    def __init__(self):
        from memory.config_manager import get_plugin_config
        self.printify_token = str(get_plugin_config("printify").get("token") or "").strip()
        cf = get_plugin_config("cloudflare")
        self.cf_account, self.cf_token = str(cf.get("account_id") or "").strip(), str(cf.get("api_token") or "").strip()

    def ask(self, prompt: str) -> Optional[dict]:
        """The agents' thinking (Trend Scout, Analyst): one Gemini call, JSON back, None if it didn't answer."""
        from core import gemini
        return gemini.as_json(prompt, tier=gemini.SMART)

    def idea(self, recent: list, trending: str = "", notes: str = "") -> dict:
        from core import gemini
        out = gemini.as_json(IDEA_PROMPT.format(recent="; ".join(recent[-40:]) or "none yet",
                                                trends=TRENDS_NOTE.format(trending) if trending else "", notes=notes),
                             tier=gemini.SMART)
        if out is None:
            raise RuntimeError("Gemini didn't answer (out of free quota?)")
        return out

    def draw(self, prompt: str) -> bytes:
        import requests
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account}/ai/run/{FLUX}",
                          headers={"Authorization": f"Bearer {self.cf_token}"}, json={"prompt": prompt, "steps": 8},
                          timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Cloudflare image failed ({r.status_code}): {r.text[:160]}")
        return base64.b64decode(r.json()["result"]["image"])

    def printify(self, method: str, path: str, body=None):
        import requests
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {self.printify_token}",
                                                          "User-Agent": "Jarvis"}, json=body, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"Printify {method} {path.split('?')[0]} failed ({r.status_code}): {r.text[:200]}")
        return r.json() if r.content else {}


class Shop:
    def __init__(self, clients=None, *, state_path: Path = STATE, art_dir: Path = ART,
                 cfg: Callable[[], dict] = settings, log: Callable[[str], None] = print,
                 notify: Optional[Callable[[str, str], None]] = None, clock: Callable[[], float] = time.time,
                 trends_fetch: Optional[Callable[[], dict]] = None, notebook_path: Optional[Path] = None):
        from worker import shop_agents
        self.c = clients or Clients()
        self.trends_fetch = trends_fetch
        self.nb_path = notebook_path or shop_agents.NOTEBOOK
        self.state_path, self.art_dir, self.cfg, self.log, self.clock = state_path, art_dir, cfg, log, clock
        self.notify = notify or (lambda kind, text: None)
        self._variants = _VARIANTS if clients is None else {}     # the real catalog is shared across passes

    # which Printify stores are connected to which sales channel
    def stores(self) -> dict:
        """{"etsy": store, "ebay": store, "popup": store} for the channels connected in Printify."""
        out = {}
        for shop in self.c.printify("GET", "shops.json"):
            channel = str(shop.get("sales_channel") or "")
            if "pop" in channel.lower():                  # the Pop-Up Store
                channel = "popup"
            if channel in CHANNELS:
                out.setdefault(channel, shop)
        return out

    def on_etsy(self, rec: dict, st: dict) -> bool:
        """Whether a design goes on Etsy: the first holiday design (or winner spin-off) of each day, up to
        etsy_per_day. Decided once per design and kept."""
        if "etsy" not in rec:
            cfg = self.cfg()
            day = datetime.fromtimestamp(rec["made"]).date()
            picked = sum(1 for d in st["designs"] if d.get("etsy") and datetime.fromtimestamp(d["made"]).date() == day)
            wanted = bool(rec.get("season") or rec.get("spinoff_of")) or not cfg.get("etsy_holiday_only")
            rec["etsy"] = wanted and picked < cfg.get("etsy_per_day", 1)
        return rec["etsy"]

    def etsy_shop(self) -> Optional[dict]:
        return self.stores().get("etsy")

    def any_shop(self) -> dict:
        shop = self.etsy_shop()
        if shop:
            return shop
        shops = self.c.printify("GET", "shops.json")
        if not shops:
            raise RuntimeError("there's no store in Printify")
        return shops[0]

    def variants(self, kind: str) -> tuple:
        """(variant ids to sell, placeholder size) for a product kind."""
        if kind in self._variants:
            return self._variants[kind]
        p = PRODUCTS[kind]
        data = self.c.printify("GET", f"catalog/blueprints/{p['blueprint']}/print_providers/{p['provider']}/variants.json")
        chosen = []
        for v in data["variants"]:
            title = v.get("title", "")
            opts = v.get("options") or {}
            if "colors" in p and opts.get("color") not in p["colors"]:
                continue
            if any(opts.get(k) != want for k, want in (p.get("match") or {}).items()):
                continue
            size = opts.get("size") or title.split(" / ")[-1]
            if "sizes" in p and size not in p["sizes"] and not any(title.startswith(s) for s in p["sizes"]):
                continue
            chosen.append(v)
        if kind == "mug":
            white = [v for v in chosen if "white" in v.get("title", "").lower()]
            chosen = white or chosen
        if not chosen:
            raise RuntimeError(f"no {p['name']} variants matched")
        front = next(ph for ph in chosen[0]["placeholders"] if ph["position"] == p.get("position", "front"))
        _AREAS[kind] = {ph["position"]: (ph["width"], ph["height"]) for ph in chosen[0]["placeholders"]}
        self._variants[kind] = ([v["id"] for v in chosen][:100], (front["width"], front["height"]))
        return self._variants[kind]

    def trending(self, st: dict) -> str:
        """The Trend Scout's picks from today's memes and internet talk (feeds fetched once a day, saved into st, which the
        caller saves; the picks are cached in the notebook)."""
        from worker import shop_agents, trends
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        cached = st.get("trends") or {}
        if cached.get("day") != today:
            found = self.trends_fetch() if self.trends_fetch else trends.fetch()
            cached = {"day": today, "text": trends.brief(found)}
            if found:
                st["trends"] = cached
        nb = shop_agents.load(self.nb_path)
        text = shop_agents.scout(nb, cached.get("text", ""), getattr(self.c, "ask", None), today, self.clock)
        shop_agents.save(nb, self.nb_path)
        return text

    # one design, start to finish
    def make_design(self, only: Optional[list] = None, spinoff: Optional[str] = None, season: Optional[str] = None,
                    style: Optional[str] = None) -> dict:
        """only: the product kinds this design goes on (e.g. ["tee"]); None means the usual set. spinoff: the id of a winning
        design to riff on instead of today's trends."""
        from worker import shop_agents
        st = load_state(self.state_path)
        recent = [d.get("niche", "") + ": " + d.get("slogan", "") for d in st["designs"]]
        parent = next((d for d in st["designs"] if d.get("id") == spinoff), None) if spinoff else None
        trending = "" if parent else self.trending(st)
        nb = shop_agents.load(self.nb_path)
        notes = shop_agents.designer_notes(nb, parent)
        if season:
            notes = SEASON_NOTE.format(name=season.title(), note=SEASONS[season]["note"]) + "\n\n" + notes
        if style == "sweater":
            notes = SWEATER_NOTE + "\n\n" + notes
            only = only or SWEATER_PRODUCTS
        last_error = None
        for _ in range(3):                                      # a refused idea gets another try
            try:
                idea = clean_idea(self.c.idea(recent, trending, notes))
                break
            except ValueError as exc:
                last_error = exc
                self.log(f"shop: idea refused: {exc}")
                shop_agents.problem(nb, f"Designer's idea was refused: {exc}", self.clock)
                shop_agents.save(nb, self.nb_path)
        else:
            raise RuntimeError(f"no usable idea in three tries ({last_error})")
        if style == "sweater":
            idea["title"] = sweater_title(idea["title"])
        from PIL import Image
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        self.art_dir.mkdir(parents=True, exist_ok=True)
        path = self.art_dir / f"{stamp}.png"
        if style == "sweater":
            from worker import sweater
            design = sweater.design(idea["slogan"] or idea["title"][:30], seed=int(self.clock()))
        else:
            raw = self.c.draw(idea["image_prompt"])
            art = cut_background(Image.open(io.BytesIO(raw)))
            design = compose(art, idea["slogan"])
            (self.art_dir / f"{stamp}-raw.jpg").write_bytes(raw)
        design.save(path, "PNG", optimize=True)
        rec = {"id": stamp, **idea, "file": str(path), "size": list(design.size), "made": self.clock(), "products": {}}
        if only:
            rec["only"] = list(only)
        if parent:
            rec["spinoff_of"] = parent["id"]
        if season:
            rec["season"] = season
        if style:
            rec["style"] = style
        st["designs"].append(rec)
        save_state(st, self.state_path)
        self.log(f"shop: designed '{idea['title']}' ({idea['slogan']})")
        nb = shop_agents.load(self.nb_path)
        shop_agents.log(nb, "designer", "publisher", f"designed \"{idea['slogan']}\" ({idea['niche']})" +
                        (f", a spin-off of \"{parent.get('slogan')}\"" if parent else ""), self.clock)
        shop_agents.save(nb, self.nb_path)
        return rec

    def upload(self, rec: dict) -> str:
        if rec.get("upload_id"):
            return rec["upload_id"]
        data = Path(rec["file"]).read_bytes()
        up = self.c.printify("POST", "uploads/images.json",
                             {"file_name": Path(rec["file"]).name, "contents": base64.b64encode(data).decode()})
        return up["id"]

    def upload_slogan(self, rec: dict) -> tuple:
        """(Printify image id, size) of the slogan-only art, made and uploaded once per design."""
        path = Path(rec["file"]).with_name(f"{rec['id']}-slogan.png")
        if not path.exists():
            slogan_art(rec.get("slogan") or rec["title"][:40]).save(path, "PNG", optimize=True)
        if not rec.get("slogan_upload_id"):
            up = self.c.printify("POST", "uploads/images.json",
                                 {"file_name": path.name, "contents": base64.b64encode(path.read_bytes()).decode()})
            rec["slogan_upload_id"] = up["id"]
        from PIL import Image
        with Image.open(path) as im:
            return rec["slogan_upload_id"], im.size

    def create(self, rec: dict, kind: str, shop_id: int, image_id: str, title_max: int = 140) -> dict:
        p = PRODUCTS[kind]
        ids, (pw, ph) = self.variants(kind)
        art_size = rec["size"]
        if p.get("art") == "slogan":
            image_id, art_size = self.upload_slogan(rec)
        if p.get("art") == "sweater":
            return self.create_sweater(rec, kind, shop_id, ids, title_max)
        images = placements(kind, image_id, art_size, (pw, ph))
        title = listing_title(rec["title"], p["name"], title_max)
        desc = (f"{rec['description']}\n\n{rec['slogan'].title()}." if rec.get("slogan") else rec["description"]) + \
            ("\n\nDesigned in our studio with the help of AI image tools, then printed on demand and shipped by our "
             "production partner. Each item is made for you after you order.")
        body = {"title": title, "description": desc, "tags": rec["tags"], "blueprint_id": p["blueprint"],
                "print_provider_id": p["provider"],
                "variants": [{"id": i, "price": 2999, "is_enabled": True} for i in ids],
                "print_areas": [{"variant_ids": ids, "placeholders": [
                    {"position": p.get("position", "front"), "images": images}]}]}
        product = self.c.printify("POST", f"shops/{shop_id}/products.json", body)
        extra = self.save_photos(rec, kind, product.get("images") or [])
        priced = [{"id": v["id"], "price": price_for(int(v.get("cost") or 0), p["profit"]), "is_enabled": True}
                  for v in product.get("variants") or [] if v["id"] in ids]
        if priced and all(v.get("cost") for v in product["variants"] if v["id"] in ids):
            self.c.printify("PUT", f"shops/{shop_id}/products/{product['id']}.json", {"variants": priced})
        return {"id": product["id"], "shop": shop_id, "price": min((v["price"] for v in priced), default=2999),
                "published": False, "extra_photos": extra}

    def create_sweater(self, rec: dict, kind: str, shop_id: int, ids: list, title_max: int) -> dict:
        """The all-over-print sweater: every panel (front, back, sleeves, cuffs, collar, waistband) is its own image."""
        from PIL import Image
        from worker import sweater
        p = PRODUCTS[kind]
        ups = rec.setdefault("panel_uploads", {})
        with Image.open(rec["file"]) as art:
            panels = sweater.panels(art.convert("RGBA"), {k: v for k, v in _AREAS[kind].items() if k not in ups},
                                    seed=int(re.sub(r"\D", "", rec["id"]) or 0))
        for pos, img in panels.items():
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            up = self.c.printify("POST", "uploads/images.json", {"file_name": f"{rec['id']}-{pos}.jpg",
                                                                 "contents": base64.b64encode(buf.getvalue()).decode()})
            ups[pos] = up["id"]
        holders = [{"position": pos, "images": [{"id": ups[pos], "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}
                   for pos in _AREAS[kind]]
        desc = f"{rec['description']}\n\n{rec['slogan'].title()}." + \
            ("\n\nA lightweight crew neck sweatshirt printed all over, sleeves and cuffs included: the knit, the lights and "
             "the stitches are printed, not real. "
             "Designed in our studio, then printed on demand and shipped by our production partner. "
             "Made for you after you order, so allow about two weeks.")
        body = {"title": listing_title(rec["title"], p["name"], title_max), "description": desc, "tags": rec["tags"],
                "blueprint_id": p["blueprint"], "print_provider_id": p["provider"],
                "variants": [{"id": i, "price": 5999, "is_enabled": True} for i in ids],
                "print_areas": [{"variant_ids": ids, "placeholders": holders}]}
        product = self.c.printify("POST", f"shops/{shop_id}/products.json", body)
        extra = self.save_photos(rec, kind, product.get("images") or [])
        priced = [{"id": v["id"], "price": price_for(int(v.get("cost") or 0), p["profit"]), "is_enabled": True}
                  for v in product.get("variants") or [] if v["id"] in ids]
        if priced and all(v.get("cost") for v in product["variants"] if v["id"] in ids):
            self.c.printify("PUT", f"shops/{shop_id}/products/{product['id']}.json", {"variants": priced})
        return {"id": product["id"], "shop": shop_id, "price": min((v["price"] for v in priced), default=5999),
                "published": False, "extra_photos": extra}

    def save_photos(self, rec: dict, kind: str, images: list) -> list:
        """Download the chosen extra photos (of the first variant) to memory/etsy_shop/photos/, plus the
        design close-up."""
        import requests
        first = next((i["variant_ids"][0] for i in images if i.get("variant_ids")), None)
        folder = self.art_dir / "photos"
        saved = []
        for name, detail in (("design", False), ("detail", True)):
            closeup = folder / f"{rec['id']}-{name}.jpg"
            if not closeup.exists() and Path(rec.get("file", "")).is_file():
                folder.mkdir(parents=True, exist_ok=True)
                design_closeup(Path(rec["file"]), closeup, detail=detail)
            if closeup.exists():
                saved.append(str(closeup))
        mine = [i for i in images if first is None or first in i.get("variant_ids", [])]
        labels = [m.group(1) for i in mine if (m := re.search(r"camera_label=([\w-]+)$", i["src"]))]
        # the chosen shots, or for a kind without a list every other angle Printify rendered (not the main
        # photo, which Etsy already has), up to Etsy's 10 with Printify's own and the two close-ups
        cams = list(EXTRA_PHOTOS[kind]) if kind in EXTRA_PHOTOS else \
            [c for c in dict.fromkeys(labels) if labels and c != labels[0]]
        for cam in cams[:MAX_MOCKUPS]:
            img = next((i for i in mine if i["src"].endswith(f"camera_label={cam}")), None)
            if not img:
                continue
            dest = folder / f"{rec['id']}-{kind}-{cam}.jpg"
            if not dest.exists():
                try:
                    r = requests.get(img["src"], headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
                    if r.status_code != 200 or len(r.content) < 5000:
                        continue
                    folder.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
                except Exception:
                    continue
            saved.append(str(dest))
        return saved

    def publish(self, product: dict) -> None:
        self.c.printify("POST", f"shops/{product['shop']}/products/{product['id']}/publish.json",
                        {"title": True, "description": True, "images": True, "variants": True, "tags": True,
                         "keyFeatures": True, "shipping_template": True})
        product["published"] = True

    def list_pending(self) -> list:
        """Put every queued design on the products it's still missing in each connected channel, and publish
        them. A channel that fails waits an hour without holding up the others."""
        stores = self.stores()
        if not stores:
            return []
        st = load_state(self.state_path)
        cfg = self.cfg()
        done = []
        on_ebay = sum(len(d.get("channels", {}).get("ebay", {})) for d in st["designs"])
        for rec in st["designs"]:
            if rec.get("failed"):
                continue
            for channel, shop in stores.items():
                retry = rec.setdefault("retry_at_by", {})
                if self.clock() < retry.get(channel, rec.get("retry_at", 0) if channel == "etsy" else 0):
                    continue
                listed = products_in(rec, channel)
                kinds = rec.get("only") or cfg["products"]
                if channel == "etsy":
                    kinds = [k for k in kinds if k in listed or
                             (self.on_etsy(rec, st) and k in (cfg.get("etsy_products") or kinds))]
                for kind in kinds:
                    if kind not in PRODUCTS:
                        continue
                    prod = listed.get(kind)
                    if channel == "ebay" and prod is None:
                        if on_ebay >= cfg.get("ebay_max", 250):
                            if st.get("ebay_full_texted") != datetime.fromtimestamp(self.clock()).strftime("%Y-%m"):
                                st["ebay_full_texted"] = datetime.fromtimestamp(self.clock()).strftime("%Y-%m")
                                self.notify("shop", f"🛍️ eBay has {on_ebay} listings, its free limit: new designs "
                                                    "now go only on the free Pop-Up Store (and Etsy's daily pick).")
                            continue
                        on_ebay += 1
                    try:
                        if prod is None or prod.get("shop") != shop["id"]:
                            rec["upload_id"] = self.upload(rec)
                            prod = listed[kind] = self.create(rec, kind, shop["id"], rec["upload_id"],
                                                              CHANNELS[channel]["title_max"])
                            save_state(st, self.state_path)
                        if not prod["published"]:
                            self.publish(prod)
                            prod["listed_at"] = self.clock()
                            done.append(f"{rec['title'][:45]} {PRODUCTS[kind]['name']} on {CHANNELS[channel]['name']} "
                                        f"(${prod['price'] / 100:.2f})")
                    except Exception as exc:
                        self.log(f"shop: {channel} {kind} for {rec['id']} failed: {exc}")
                        if channel not in rec.setdefault("failure_texted", []):
                            rec["failure_texted"].append(channel)
                            self.notify("shop", f"⚠️ Couldn't list '{rec['title'][:50]}' on "
                                                f"{CHANNELS[channel]['name']}: {str(exc)[:120]}. I'll retry in an hour.")
                        rec.setdefault("errors", []).append(f"{channel} {kind}: {str(exc)[:200]}")
                        from worker import shop_agents
                        nb = shop_agents.load(self.nb_path)
                        shop_agents.problem(nb, f"Publisher couldn't list \"{rec.get('slogan')}\" {kind} on {channel}: {str(exc)[:120]}",
                                            self.clock)
                        shop_agents.save(nb, self.nb_path)
                        retry[channel] = self.clock() + 3600         # an hour, not every pass
                        if len(rec["errors"]) >= 12:
                            rec["failed"] = True
                        save_state(st, self.state_path)
                        break
                    save_state(st, self.state_path)
        return done

    def new_orders(self) -> list:
        stores = self.stores()
        if not stores:
            return []
        st = load_state(self.state_path)
        started = st.setdefault("orders_started_by", {"etsy": True} if st.get("orders_started") else {})
        lines = []
        retail = {p.get("id"): p.get("price") for rec in st["designs"] for ch in CHANNELS
                  for p in products_in(rec, ch).values() if p.get("id")}
        design_of = {p.get("id"): rec.get("id") for rec in st["designs"] for ch in CHANNELS
                     for p in products_in(rec, ch).values() if p.get("id")}
        for channel, shop in stores.items():
            data = self.c.printify("GET", f"shops/{shop['id']}/orders.json?limit=20")
            status = st.setdefault("order_status", {})
            for o in data.get("data") or []:
                items = ", ".join(str((li.get("metadata") or {}).get("title") or "an item")[:50]
                                  for li in o.get("line_items") or [])
                if o["id"] not in st["orders_seen"]:
                    st["orders_seen"].append(o["id"])
                    status[o["id"]] = o.get("status")
                    if not started.get(channel):                # the first look at a channel is a baseline
                        continue
                    prices = [retail.get(li.get("product_id")) for li in o.get("line_items") or []]
                    total = sum(p * int(li.get("quantity") or 1) for p, li in zip(prices, o.get("line_items") or [])
                                if p) / 100                       # what the buyer paid us (Printify's total is its cost)
                    designs = sorted({design_of[li.get("product_id")] for li in o.get("line_items") or [] if li.get("product_id") in design_of})
                    st.setdefault("sales_log", []).append({"at": self.clock(), "channel": channel, "items": items,
                                                           "price": total, "designs": designs})
                    st["sales_log"] = st["sales_log"][-500:]
                    lines.append(f"💰 SALE on {CHANNELS[channel]['name']}! {items}" +
                                 (f" (${total:.2f})" if total else "") + ". Printify will print and ship it.")
                    from worker import shop_agents
                    nb = shop_agents.load(self.nb_path)
                    for did in designs:
                        win = shop_agents.on_sale(nb, st, did, total / max(1, len(designs)), self.clock)
                        if win:
                            lines.append(win)
                    shop_agents.save(nb, self.nb_path)
                    continue
                if o.get("status") != status.get(o["id"]):
                    status[o["id"]] = o.get("status")
                    line = order_update(o, items, CHANNELS[channel]["name"])
                    if line:
                        lines.append(line)
            started[channel] = True
        st["orders_started"] = True
        st["orders_seen"] = st["orders_seen"][-500:]
        save_state(st, self.state_path)
        return lines


class ShopService:
    """The worker's shop thread: today's designs at `time`, listing whatever is queued, and new orders."""

    PASS_SECONDS = 300

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="etsy-shop").start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self, shop: Optional[Shop] = None, now: Optional[datetime] = None) -> None:
        cfg = settings()
        if not cfg["enabled"]:
            return
        shop = shop or Shop(log=self.log, notify=self.notify)
        if not (shop.c.printify_token and shop.c.cf_token):
            return
        now = now or datetime.now()
        st = load_state(shop.state_path)
        today = now.date().isoformat()
        h, m = (int(x) for x in str(cfg["time"]).split(":"))
        made_today = sum(1 for d in st["designs"] if datetime.fromtimestamp(d["made"]).date().isoformat() == today
                         and not d.get("season"))
        season = season_for(now.date())
        holiday_today = sum(1 for d in st["designs"] if datetime.fromtimestamp(d["made"]).date().isoformat() == today
                            and d.get("season"))
        # the holiday line's design(s), from half an hour after the daily one
        holiday = bool(season) and holiday_today < cfg["holiday_per_day"] and \
            now.hour * 60 + now.minute >= h * 60 + m + 30 + holiday_today * 240
        queued = int(st.get("make_now") or 0)              # "make N new products now", from the plugin; one per pass
        spin_q = list(st.get("make_spinoff") or [])        # spin-offs of a design that just sold, queued by the Analyst
        cooling = time.time() - st.get("last_fail_at", 0) < 3600
        # N designs a day are spread from the start time to midnight (4 a day at 06:00: 06:00, 10:30, 15:00, 19:30)
        n = cfg["designs_per_day"]
        gap = (24 * 60 - (h * 60 + m)) / n if n else 0
        daily = made_today < n and now.hour * 60 + now.minute >= h * 60 + m + made_today * gap
        if not cooling and (spin_q or queued or daily or holiday):
            from worker import shop_agents
            try:
                if spin_q:
                    shop.make_design(spinoff=spin_q[0])
                    st = load_state(shop.state_path)
                    st["make_spinoff"] = (st.get("make_spinoff") or [])[1:]
                    st["spinoffs_made"] = int(st.get("spinoffs_made") or 0) + 1
                    save_state(st, shop.state_path)
                    queued = 0
                elif queued:
                    shop.make_design(only=st.get("make_only"))
                elif holiday and not daily:
                    # each Christmas day starts with an ugly-sweater design (sweater, tee, hoodie)
                    # (and its last one too: the user liked the line, 2026-10-26)
                    if season == "christmas" and holiday_today in (0, cfg["holiday_per_day"] - 1):
                        shop.make_design(season=season, style="sweater")
                    else:
                        shop.make_design(only=SEASONS[season]["products"], season=season)
                else:                                      # the daily design: every other one riffs on a winner, if there is one
                    win = shop_agents.next_spinoff(shop_agents.load(shop.nb_path), bool(st.get("last_was_spinoff")),
                                                   int(st.get("spinoffs_made") or 0))
                    shop.make_design(spinoff=win)
                    st = load_state(shop.state_path)
                    st["last_was_spinoff"] = bool(win)
                    if win:
                        st["spinoffs_made"] = int(st.get("spinoffs_made") or 0) + 1
                    save_state(st, shop.state_path)
                if queued:
                    st = load_state(shop.state_path)
                    st["make_now"] = queued - 1
                    if queued == 1:
                        st.pop("make_only", None)
                    save_state(st, shop.state_path)
            except Exception as exc:
                self.log(f"shop: today's design failed: {exc}")
                busy = "didn't answer" in str(exc)                 # Gemini overloaded for a while: try again in 15 minutes, not an hour
                st["last_fail_at"] = time.time() - (2700 if busy else 0)
                save_state(st, shop.state_path)
                if st.get("last_fail_day") != today:
                    st["last_fail_day"] = today
                    save_state(st, shop.state_path)
                    self.notify("system", f"⚠️ The Etsy shop couldn't make today's design: {str(exc)[:120]}")
                return
        listed = shop.list_pending()
        if listed:
            self.notify("shop", "🛍️ Just listed: " + "; ".join(listed[:6]) +
                        (f"; and {len(listed) - 6} more" if len(listed) > 6 else "") + ".")
        for line in shop.new_orders():
            self.notify("shop", line)
        self.storefront(shop, today, force=bool(listed))
        self.etsy_photos(shop)
        self.video_slot(shop, now)
        self.evening_review(shop, now)

    REVIEW_HOUR = 21

    def evening_review(self, shop: "Shop", now: datetime) -> bool:
        """The Analyst's nightly rewrite of the agents' notebook."""
        from worker import shop_agents
        nb = shop_agents.load(shop.nb_path)
        if now.hour < self.REVIEW_HOUR or nb.get("reviewed_day") == now.date().isoformat() or not hasattr(shop.c, "ask"):
            return False
        nb["reviewed_day"] = now.date().isoformat()          # one try a day, even if Gemini doesn't answer
        shop_agents.save(nb, shop.nb_path)
        ok = shop_agents.review(nb, load_state(shop.state_path), shop.c.ask, shop.clock)
        shop_agents.save(nb, shop.nb_path)
        self.log("shop: the Analyst " + ("rewrote the notebook" if ok else "couldn't review today"))
        return ok

    def video_slot(self, shop: "Shop", now: datetime) -> str:
        """At each of the day's video times: make the next video and post it (Instagram) or text it to you."""
        times = [t.strip() for t in str(settings()["video_times"]).split(",") if t.strip()]
        st = load_state(shop.state_path)
        today = now.date().isoformat()
        done = st.setdefault("video_slots", {}).get(today, [])
        due = [i for i, t in enumerate(times) if i not in done and now.strftime("%H:%M") >= t]
        if not due:
            return ""
        slot = due[0]
        st["video_slots"] = {today: done + due}           # missed slots (Mac asleep) are skipped, not stacked up
        save_state(st, shop.state_path)
        listed = [d for d in st["designs"] if any(p.get("published") for p in d.get("products", {}).values())]
        if not listed:
            return ""
        from worker import videos
        fmt = videos.FORMATS[slot % len(videos.FORMATS)]
        made = {(v["design"], v["format"]) for v in st.setdefault("videos", [])}
        fresh = [d for d in reversed(listed) if (d["id"], fmt) not in made]
        rec = fresh[0] if fresh else min(listed, key=lambda d: sum(v["design"] == d["id"] for v in st["videos"]))
        try:
            from worker import storefront
            video = videos.make(rec, fmt, shop.art_dir / "videos", storefront.settings()["name"], printify=shop.c.printify)
        except Exception as exc:
            self.log(f"videos: couldn't make a {fmt} video: {exc}")
            return "failed"
        video["made"] = time.time()
        st = load_state(shop.state_path)
        for d in st["designs"]:
            if d["id"] == rec["id"] and rec.get("video"):
                d["video"] = rec["video"]                    # keep the copy for the next formats
        st.setdefault("videos", []).append(video)
        save_state(st, shop.state_path)
        return self.deliver(video, shop, slot + 1, len(times))

    def deliver(self, video: dict, shop: "Shop", n: int, of: int) -> str:
        """Texts the video to the user (Jarvis never posts to their social accounts)."""
        from core import phone_alerts as pa
        cfg = pa.settings()
        if not cfg["enabled"] or "videos" in cfg["off"]:
            return "off"
        try:
            pa.send_file(video["file"], f"🎬 Video {n} of {of} for today. Not posted yet: save it and post it "
                                        f"yourself on TikTok, Instagram Reels or YouTube Shorts. "
                                        f"Copy this caption:\n{video['caption']}", cfg)
            self.mark(shop, video, texted=True)
            return "texted"
        except Exception as exc:
            self.log(f"videos: couldn't text the video: {exc}")
            return "failed"

    def mark(self, shop: "Shop", video: dict, **kv) -> None:
        st = load_state(shop.state_path)
        for v in st.get("videos", []):
            if v["file"] == video["file"]:
                v.update(kv)
        save_state(st, shop.state_path)

    def more_photos(self, shop: "Shop", limit: int = 10) -> None:
        """Products listed before every angle was saved (only the two close-ups) get the angles Printify
        still has, once each."""
        st = load_state(shop.state_path)
        n = 0
        for rec in st["designs"]:
            for kind, prod in rec.get("products", {}).items():
                if n >= limit or prod.get("photos_added") or prod.get("photos_checked") or len(prod.get("extra_photos") or []) > 2:
                    continue
                n += 1
                try:
                    p = shop.c.printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json")
                except Exception:
                    continue
                prod["extra_photos"] = list(dict.fromkeys((prod.get("extra_photos") or []) +
                                                          shop.save_photos(rec, kind, p.get("images") or [])))
                prod["photos_checked"] = True
        if n:
            save_state(st, shop.state_path)

    def etsy_photos(self, shop: "Shop") -> None:
        """Add the saved extra photos to each Etsy listing, once Jarvis is connected to Etsy itself."""
        from worker import etsy_api
        self.more_photos(shop)
        if not etsy_api.connected():
            return
        st = load_state(shop.state_path)
        n = 0
        for rec in st["designs"]:
            for kind, prod in rec.get("products", {}).items():
                if not prod.get("published") or prod.get("photos_added") or not prod.get("extra_photos"):
                    continue
                if not prod.get("url"):
                    try:
                        p = shop.c.printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json")
                        prod["url"] = (p.get("external") or {}).get("handle") or ""
                    except Exception:
                        continue
                listing = etsy_api.listing_id(prod.get("url"))
                if not listing:
                    continue                                   # Etsy hasn't got it yet; next pass
                try:
                    n += etsy_api.add_photos(listing, prod["extra_photos"])
                    prod["photos_added"] = True
                except Exception as exc:
                    self.log(f"shop: photos for Etsy listing {listing} failed: {exc}")
                    save_state(st, shop.state_path)
                    return                                     # try again next pass
                save_state(st, shop.state_path)
        if n:
            self.log(f"shop: added {n} photos to Etsy listings")

    def storefront(self, shop: "Shop", today: str, force: bool = False) -> None:
        """Rebuild the public storefront (worker/storefront.py) after new listings, else once a day."""
        st = load_state(shop.state_path)
        if not force and st.get("storefront_day") == today:
            return
        st["storefront_day"] = today
        try:
            from worker import storefront
            storefront.publish(st["designs"], shop.c.printify, self.log)    # caches each listing's link and photos
        except Exception as exc:
            self.log(f"storefront: {exc}")
        save_state(st, shop.state_path)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"shop: pass failed: {exc!r}")
            self._stop.wait(self.PASS_SECONDS)
