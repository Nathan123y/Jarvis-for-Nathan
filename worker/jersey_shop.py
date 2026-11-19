"""The personalised basketball jersey: listed on every connected shop, kept apart from the daily design pipeline.

State lives in memory/jersey.json: {"products": {channel: {id, shop, price, published}}, "orders": {...}}.
Nothing here publishes unless asked to (publish=True), because a buyer's name has to reach the printer: see orders().
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Optional

from worker import jersey
from worker.shop import CHANNELS, Clients, Shop, price_for

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "memory" / "jersey.json"
BLUEPRINT, PROVIDER = 1209, 83               # Unisex Basketball Jersey (AOP), Subliminator
PROFIT = 15.0
SEED = 7
TITLES = {
    "etsy": "Basketball Jersey, Navy Red Cream Star and Flames Sports Jersey, Birthday Gift for Basketball Player, Streetwear Jersey for Men Women Teens",
    "popup": "Basketball Jersey, Navy Red Cream Star and Flames Sports Jersey, Birthday Gift for Basketball Player",
    "ebay": "Basketball Jersey Navy Red Cream Star Flames Unisex Streetwear Sports Jersey Gift",
    "tiktok": "Basketball Jersey Navy Red Star Flames Unisex Streetwear Sports Jersey Gift",
}
TAGS = ["basketball jersey", "streetwear jersey", "sports jersey", "basketball gift", "gift for him",
        "gift for teen boy", "basketball fan", "mens jersey", "womens jersey", "unisex jersey", "birthday gift",
        "star jersey", "flame jersey"]


def description(channel: str) -> str:
    return (
        "A basketball jersey printed all over in navy, red, sky blue and cream, with a big star outline and flames. "
        "The front says BUCKET GETTER over a number 10, and the back has a big 10.\n\n"
        "The details:\n"
        "- 100% recycled moisture-wicking polyester mesh, light (4.8 oz), a standard unisex fit\n"
        "- Ribbed black collar and armholes, sewn-in size label\n"
        "- Sizes XS to 4XL (it runs like a regular basketball jersey)\n"
        "- Printed to order after you buy, so allow about 1-2 weeks including shipping\n\n"
        "This is an original design. It is not made by, licensed by or connected to any team, league or player."
    )


def load(path: Path = STATE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"products": {}, "orders": {}}


def save(st: dict, path: Path = STATE) -> None:
    path.write_text(json.dumps(st, indent=1))


def _upload(c: Clients, img, name: str) -> str:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return c.printify("POST", "uploads/images.json", {"file_name": name, "contents": base64.b64encode(buf.getvalue()).decode()})["id"]


def list_everywhere(publish: bool = False, only: Optional[list] = None, log=print) -> dict:
    """Create the jersey as a product in each connected shop (a draft unless publish=True) and remember the ids."""
    c = Clients()
    shop = Shop(c)
    st = load()
    stores = shop.stores()
    art = st.setdefault("art", {})
    if "front" not in art:
        log("drawing the front and back…")
        art["front"] = _upload(c, jersey.front(seed=SEED), "jersey-front.jpg")
        art["back"] = _upload(c, jersey.back(seed=SEED), "jersey-back.jpg")
        save(st)
    variants = c.printify("GET", f"catalog/blueprints/{BLUEPRINT}/print_providers/{PROVIDER}/variants.json")["variants"]
    ids = [v["id"] for v in variants]
    areas = [{"variant_ids": ids, "placeholders": [
        {"position": pos, "images": [{"id": art[pos], "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]} for pos in ("front", "back")]}]
    for channel, store in stores.items():
        if only and channel not in only:
            continue
        prod = st["products"].get(channel)
        if not prod:
            body = {"title": TITLES[channel][:CHANNELS[channel]["title_max"]], "description": description(channel), "tags": TAGS,
                    "blueprint_id": BLUEPRINT, "print_provider_id": PROVIDER,
                    "variants": [{"id": i, "price": 4199, "is_enabled": True} for i in ids], "print_areas": areas}
            made = c.printify("POST", f"shops/{store['id']}/products.json", body)
            cost = {v["id"]: int(v.get("cost") or 0) for v in made["variants"]}
            priced = [{"id": i, "price": price_for(cost[i], PROFIT), "is_enabled": True} for i in ids if cost.get(i)]
            c.printify("PUT", f"shops/{store['id']}/products/{made['id']}.json", {"variants": priced})
            prod = st["products"][channel] = {"id": made["id"], "shop": store["id"], "price": min(p["price"] for p in priced),
                                              "published": False}
            save(st)
            log(f"{CHANNELS[channel]['name']}: draft {made['id']} at ${prod['price'] / 100:.2f}")
        if publish and not prod["published"]:
            shop.publish(prod)
            save(st)
            log(f"{CHANNELS[channel]['name']}: published")
    return st


def is_jersey_order(order: dict, st: Optional[dict] = None) -> bool:
    st = st or load()
    mine = {p["id"] for p in st["products"].values()}
    return any(li.get("product_id") in mine for li in order.get("line_items") or [])


if __name__ == "__main__":
    import sys
    list_everywhere(publish="--publish" in sys.argv)
