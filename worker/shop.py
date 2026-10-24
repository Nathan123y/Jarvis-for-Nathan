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
    "mug": {"name": "Mug", "blueprint": 478, "provider": 99, "profit": 6.0, "sizes": ("11oz",), "fill": 0.9},
    "sticker": {"name": "Sticker", "blueprint": 400, "provider": 99, "profit": 2.5, "fill": 1.0},
}
# Sales channels connected in Printify that every design is listed on. eBay titles stop at 80 characters.
CHANNELS = {"etsy": {"name": "Etsy", "title_max": 140}, "ebay": {"name": "eBay", "title_max": 80}}
# Printify renders dozens of product photos but sends Etsy one or two, and only says what they are when the
# product is created; these are saved then, for the listing (once Jarvis can reach Etsy) and the storefront.
# Left out: the folded shirt (it hides the design) and the mug scenes (they look at the side of the mug,
# where a centred design wraps out of sight). Every listing also gets a close-up of the design itself.
EXTRA_PHOTOS = {"tee": ("person-1", "person-2", "lifestyle", "hanging-1", "duo"),
                "mug": (),
                "sticker": ("context-1", "context-2")}
DEFAULTS = {"enabled": True, "designs_per_day": 1, "time": "10:00", "products": ["tee", "mug", "sticker"]}

BLOCKED = re.compile(r"(?<![a-z])(" + "|".join([
    "disney", "pixar", "marvel", "dc comics", "batman", "superman", "spider-?man", "avengers", "star wars", "jedi",
    "harry potter", "hogwarts", "pokemon", "pikachu", "nintendo", "mario", "zelda", "sonic", "minecraft", "fortnite",
    "barbie", "hello kitty", "sanrio", "snoopy", "peanuts", "looney", "mickey", "minnie", "simpsons", "sesame",
    "nike", "adidas", "gucci", "chanel", "louis vuitton", "supreme", "coca-?cola", "pepsi", "starbucks", "mcdonald",
    "nfl", "nba", "mlb", "nhl", "fifa", "ncaa", "olympic", "taylor swift", "swiftie", "beyonce", "elvis", "beatles",
    "friends tv", "the office", "stranger things", "game of thrones", "lord of the rings", "hobbit", "grinch",
    "dr\\.? seuss", "winnie", "bluey", "paw patrol", "peppa", "baby shark", "yoda", "grogu", "trump", "biden",
    "harley", "jeep", "ford", "chevy", "john deere", "yeti", "stanley cup", "lego", "crocs",
]) + r")(?![a-z])", re.I)

IDEA_PROMPT = """You design products for an Etsy print-on-demand shop that sells T-shirts, mugs and stickers.
Invent ONE new design that a specific group of people would buy for themselves or as a gift: a hobby,
job, pet, place type, life stage or inside joke (nurses, teachers, gardeners, cat moms, hikers, anglers,
bookworms, bakers, new dads, plant lovers, retirees, nerdy engineers, and so on, or something fresher).

Rules, all required:
- No brands, trademarks, logos, characters, celebrities, sports teams, movies, TV, games, songs, quotes or
  slogans someone else owns, politics or religion. Original ideas only.
- The illustration: one cute or funny character or object, simple bold flat vector style, thick dark
  outlines, a few bright colours, centred, isolated on a plain pure white background, no text, letters or
  numbers in the picture.
- The slogan is printed under the picture by us: 2 to 6 words, funny or heartfelt, ALL CAPS, plain ASCII.
- Title: what buyers type into Etsy search, 60-120 characters, no product type (we add "T-Shirt" etc.).
- Exactly 13 tags, each 20 characters or fewer, lowercase, what buyers search for.
- Don't repeat these recent designs: {recent}

Reply with JSON only:
{{"niche": "...", "image_prompt": "...", "slogan": "...", "title": "...", "description": "2-3 friendly sentences for the listing", "tags": ["...", ...]}}"""


# ── small pieces ─────────────────────────────────────────────────────────────
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
    slogan = re.sub(r"[^A-Z0-9 '!?&.,-]", "", str(idea.get("slogan") or "").upper()).strip()
    return {"niche": str(idea.get("niche") or "")[:80], "image_prompt": str(idea["image_prompt"])[:900],
            "slogan": " ".join(slogan.split()[:6]), "title": " ".join(str(idea["title"]).split())[:110],
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


def listing_title(base: str, product: str, limit: int) -> str:
    """'<title> <product>' within the channel's limit, cut at a word so the product name always fits."""
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
    return [{"id": image_id, "x": 0.5, "y": 0.5, "scale": round(min(1.0, (ph / pw) * (w / h)) * fill, 3), "angle": 0}]


# ── the services ─────────────────────────────────────────────────────────────
class Clients:
    """Gemini, Cloudflare and Printify, each replaceable in tests."""

    def __init__(self):
        from memory.config_manager import get_plugin_config
        self.printify_token = str(get_plugin_config("printify").get("token") or "").strip()
        cf = get_plugin_config("cloudflare")
        self.cf_account, self.cf_token = str(cf.get("account_id") or "").strip(), str(cf.get("api_token") or "").strip()

    def idea(self, recent: list) -> dict:
        from core import gemini
        out = gemini.as_json(IDEA_PROMPT.format(recent="; ".join(recent[-40:]) or "none yet"), tier=gemini.SMART)
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
                 notify: Optional[Callable[[str, str], None]] = None, clock: Callable[[], float] = time.time):
        self.c = clients or Clients()
        self.state_path, self.art_dir, self.cfg, self.log, self.clock = state_path, art_dir, cfg, log, clock
        self.notify = notify or (lambda kind, text: None)
        self._variants: dict = {}

    # which Printify stores are connected to which sales channel
    def stores(self) -> dict:
        """{"etsy": store, "ebay": store} for the channels connected in Printify."""
        out = {}
        for shop in self.c.printify("GET", "shops.json"):
            if shop.get("sales_channel") in CHANNELS:
                out.setdefault(shop["sales_channel"], shop)
        return out

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
            size = opts.get("size") or title.split(" / ")[-1]
            if "sizes" in p and size not in p["sizes"] and not any(title.startswith(s) for s in p["sizes"]):
                continue
            chosen.append(v)
        if kind == "mug":
            white = [v for v in chosen if "white" in v.get("title", "").lower()]
            chosen = white or chosen
        if not chosen:
            raise RuntimeError(f"no {p['name']} variants matched")
        front = next(ph for ph in chosen[0]["placeholders"] if ph["position"] == "front")
        self._variants[kind] = ([v["id"] for v in chosen][:100], (front["width"], front["height"]))
        return self._variants[kind]

    # one design, start to finish
    def make_design(self) -> dict:
        st = load_state(self.state_path)
        recent = [d.get("niche", "") + ": " + d.get("slogan", "") for d in st["designs"]]
        last_error = None
        for _ in range(3):                                      # a refused idea gets another try
            try:
                idea = clean_idea(self.c.idea(recent))
                break
            except ValueError as exc:
                last_error = exc
                self.log(f"shop: idea refused: {exc}")
        else:
            raise RuntimeError(f"no usable idea in three tries ({last_error})")
        from PIL import Image
        raw = self.c.draw(idea["image_prompt"])
        art = cut_background(Image.open(io.BytesIO(raw)))
        design = compose(art, idea["slogan"])
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        self.art_dir.mkdir(parents=True, exist_ok=True)
        path = self.art_dir / f"{stamp}.png"
        design.save(path, "PNG", optimize=True)
        (self.art_dir / f"{stamp}-raw.jpg").write_bytes(raw)
        rec = {"id": stamp, **idea, "file": str(path), "size": list(design.size), "made": self.clock(), "products": {}}
        st["designs"].append(rec)
        save_state(st, self.state_path)
        self.log(f"shop: designed '{idea['title']}' ({idea['slogan']})")
        return rec

    def upload(self, rec: dict) -> str:
        if rec.get("upload_id"):
            return rec["upload_id"]
        data = Path(rec["file"]).read_bytes()
        up = self.c.printify("POST", "uploads/images.json",
                             {"file_name": Path(rec["file"]).name, "contents": base64.b64encode(data).decode()})
        return up["id"]

    def create(self, rec: dict, kind: str, shop_id: int, image_id: str, title_max: int = 140) -> dict:
        p = PRODUCTS[kind]
        ids, (pw, ph) = self.variants(kind)
        images = placements(kind, image_id, rec["size"], (pw, ph))
        title = listing_title(rec["title"], p["name"], title_max)
        desc = (f"{rec['description']}\n\n{rec['slogan'].title()}." if rec.get("slogan") else rec["description"]) + \
            ("\n\nDesigned in our studio with the help of AI image tools, then printed on demand and shipped by our "
             "production partner. Each item is made for you after you order.")
        body = {"title": title, "description": desc, "tags": rec["tags"], "blueprint_id": p["blueprint"],
                "print_provider_id": p["provider"],
                "variants": [{"id": i, "price": 2999, "is_enabled": True} for i in ids],
                "print_areas": [{"variant_ids": ids, "placeholders": [
                    {"position": "front", "images": images}]}]}
        product = self.c.printify("POST", f"shops/{shop_id}/products.json", body)
        extra = self.save_photos(rec, kind, product.get("images") or [])
        priced = [{"id": v["id"], "price": price_for(int(v.get("cost") or 0), p["profit"]), "is_enabled": True}
                  for v in product.get("variants") or [] if v["id"] in ids]
        if priced and all(v.get("cost") for v in product["variants"] if v["id"] in ids):
            self.c.printify("PUT", f"shops/{shop_id}/products/{product['id']}.json", {"variants": priced})
        return {"id": product["id"], "shop": shop_id, "price": min((v["price"] for v in priced), default=2999),
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
        for cam in EXTRA_PHOTOS.get(kind, ()):
            img = next((i for i in images if i["src"].endswith(f"camera_label={cam}")
                        and (first is None or first in i.get("variant_ids", []))), None)
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
        done = []
        for rec in st["designs"]:
            if rec.get("failed"):
                continue
            for channel, shop in stores.items():
                retry = rec.setdefault("retry_at_by", {})
                if self.clock() < retry.get(channel, rec.get("retry_at", 0) if channel == "etsy" else 0):
                    continue
                listed = products_in(rec, channel)
                for kind in self.cfg()["products"]:
                    if kind not in PRODUCTS:
                        continue
                    prod = listed.get(kind)
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
                        rec.setdefault("errors", []).append(f"{channel} {kind}: {str(exc)[:200]}")
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
        for channel, shop in stores.items():
            data = self.c.printify("GET", f"shops/{shop['id']}/orders.json?limit=20")
            for o in data.get("data") or []:
                if o["id"] in st["orders_seen"]:
                    continue
                st["orders_seen"].append(o["id"])
                if not started.get(channel):                    # the first look at a channel is a baseline
                    continue
                items = ", ".join(str((li.get("metadata") or {}).get("title") or "an item")[:50]
                                  for li in o.get("line_items") or [])
                total = (o.get("total_price") or 0) / 100
                lines.append(f"🛍️ New {CHANNELS[channel]['name']} order: {items}" +
                             (f" (${total:.2f})" if total else "") + ". Printify will print and ship it.")
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
        shop = shop or Shop(log=self.log)
        if not (shop.c.printify_token and shop.c.cf_token):
            return
        now = now or datetime.now()
        st = load_state(shop.state_path)
        today = now.date().isoformat()
        h, m = (int(x) for x in str(cfg["time"]).split(":"))
        made_today = sum(1 for d in st["designs"] if datetime.fromtimestamp(d["made"]).date().isoformat() == today)
        asked = bool(st.get("make_now"))                    # "make a new product now", from the plugin
        cooling = time.time() - st.get("last_fail_at", 0) < 3600
        if asked or (not cooling and now.hour * 60 + now.minute >= h * 60 + m and made_today < cfg["designs_per_day"]):
            if asked:
                st["make_now"] = False
                save_state(st, shop.state_path)
            try:
                shop.make_design()
            except Exception as exc:
                self.log(f"shop: today's design failed: {exc}")
                st["last_fail_at"] = time.time()
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

    def etsy_photos(self, shop: "Shop") -> None:
        """Add the saved extra photos to each Etsy listing, once Jarvis is connected to Etsy itself."""
        from worker import etsy_api
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
