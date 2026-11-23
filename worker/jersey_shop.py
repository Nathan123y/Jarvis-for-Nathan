"""The jersey line: art the user makes in ChatGPT, listed by Jarvis after the user approves it from their phone.

The flow (user, 2026-11-19):
1. The user drops a concept's flat print art in memory/jersey_art/inbox/<concept>/: front.png and back.png (tall 2:3),
   plus sleeve.png for a soccer jersey (no sleeve = basketball). An optional info.txt: "title: ..." and/or "soccer".
2. Each worker pass (Runtime.jerseys) picks it up: fits the art to the jersey's print areas, makes a draft on the
   Pop-Up Store for Printify's mockups and texts the user the front and back.
3. The user approves or rejects it (from Claude on their phone): python3 -m worker.jersey_shop approve <concept>.
   That only marks it; the worker lists it on its next pass, so the shop state has one writer.
4. Approved: TikTok Shop (the best audience so far: it's at its 100-listing limit, so the oldest never-sold shirt
   there makes room), eBay (the same: the oldest never-sold listing goes), the Pop-Up Store and so Pinterest (the
   storefront feed pins it), and Etsy slowly: one jersey every ETSY_EVERY_DAYS.

No real team marks, names or players are ever listed: the user's own made-up clubs only.
State: memory/jersey.json {"designs": [rec, ...]}; a rec looks like a shop design (products/channels) so the
storefront can pin it.
"""
from __future__ import annotations

import base64
import io
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "memory" / "jersey.json"
INBOX = ROOT / "memory" / "jersey_art" / "inbox"
DESIGNS = ROOT / "memory" / "jersey_art" / "designs"
ETSY_EVERY_DAYS = 3
TIKTOK_MAX = 100
# both made and shipped in the US (Printify charges $6.39 to ship either; the first jersey tried, blueprint 1209 from
# Subliminator, cost $20.79 to ship)
KINDS = {
    "basketball": {"name": "Basketball Jersey", "blueprint": 949, "provider": 47, "profit": 13.0,
                   "areas": {"front": "front", "back": "back"}},
    "soccer": {"name": "Soccer Jersey", "blueprint": 1332, "provider": 99, "profit": 13.0,
               "areas": {"front": "front", "back": "back", "right_sleeve": "sleeve", "left_sleeve": "sleeve",
                         "Collar": "sleeve"}},
}
TAGS = {
    "basketball": ["basketball jersey", "streetwear jersey", "vintage jersey", "retro jersey", "sports jersey",
                   "y2k streetwear", "gift for him", "gift for teen boy", "basketball fan", "mens jersey",
                   "unisex jersey", "birthday gift", "hoop jersey"],
    "soccer": ["soccer jersey", "football shirt", "vintage jersey", "retro soccer shirt", "streetwear jersey",
               "y2k streetwear", "sports jersey", "gift for him", "gift for teen boy", "soccer fan", "unisex jersey",
               "birthday gift", "blokecore"],
}
AI_NOTE = ("Designed by us with the help of AI image tools, then printed on demand and shipped by our production "
           "partner. Each jersey is made for you after you order.")
ORIGINAL = "An original design for a made-up club. It is not made by, licensed by or connected to any team, league or player."


def load(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("designs", [])
    return st


def save(st: dict, path: Path = STATE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def slug_of(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "jersey"


def find(st: dict, name: str) -> Optional[dict]:
    want = slug_of(name)
    return next((r for r in st["designs"] if r["slug"] == want), None)


# On a worn jersey only about the middle 3/4 of a front or back panel shows from the front (Printify's model photos,
# 2026-11-19: ChatGPT's lettering spans ~80% of its image and was cut off at the sides). The art is shrunk to this share
# of the panel's width and the rest is filled by mirroring its own edges, so the pattern still runs to the seams.
PANEL_INSET = 0.74


def fit(img, size: tuple, inset: float = 1.0):
    """The art fitted to a print area: centred, its full width shrunk to `inset` of the area's width (the edges filled
    by mirroring the art outward; cropped top and bottom if it's too tall), at the area's full resolution."""
    import numpy as np
    from PIL import Image, ImageFilter
    img = img.convert("RGB")
    w, h = img.size
    tw, th = size
    ratio = tw / th
    W = max(int(round(w / inset)), 1)
    H = int(round(W / ratio))
    if H < h:                                   # too tall for the area at this width: trim top and bottom evenly
        top = (h - H) // 2
        img = img.crop((0, top, w, top + H))
        h = H
    if W > w or H > h:
        arr = np.asarray(img)
        left, top = (W - w) // 2, (H - h) // 2
        arr = np.pad(arr, ((top, H - h - top), (left, W - w - left), (0, 0)), mode="symmetric")
        img = Image.fromarray(arr)
    out = img.resize(size, Image.LANCZOS)
    return out.filter(ImageFilter.UnsharpMask(radius=3, percent=60, threshold=2)) if tw > w * 1.5 else out


def _art_file(folder: Path, stem: str) -> Optional[Path]:
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def read_info(folder: Path) -> dict:
    info = {}
    p = folder / "info.txt"
    if p.is_file():
        text = p.read_text(errors="replace")
        n = re.search(r"name:\s*(.+)", text, re.I)
        if n:
            info["concept"] = n.group(1).strip()[:40]
        m = re.search(r"title:\s*(.+)", text, re.I)
        if m:
            info["title"] = m.group(1).strip()[:140]
        if re.search(r"\bsoccer\b|\bfootball shirt\b", text, re.I):
            info["kind"] = "soccer"
    return info


def titles(rec: dict) -> dict:
    """One short, clickable title on every marketplace (user, 2026-11-22: "not too many words"): the club name, one
    hook word and the garment, e.g. "North Pole Christmas Basketball Jersey" or "Starfire Vintage Soccer Jersey"."""
    name, kind = rec["concept"].title(), KINDS[rec["kind"]]["name"]
    hook = "Christmas" if "christmas" in (rec.get("title") or "").lower() else "Vintage"
    t = f"{name} {hook} {kind}"
    return {"etsy": t, "popup": t, "ebay": t[:80], "tiktok": t[:80]}


def description(rec: dict, facts: list) -> str:
    kind = KINDS[rec["kind"]]["name"].lower()
    lines = [f"{rec['concept'].upper()}: a vintage-style {kind} printed all over, front and back, in the early-2000s "
             "streetwear look: flowing waves, warped stripes, washed colours and cracked, faded-ink lettering "
             "(the cracks and fading are printed; the jersey is new).", ""]
    if facts:
        lines += ["The details:"] + [f"- {f}" for f in facts] + [""]
    lines += ["Printed to order after you buy, so allow about 1-2 weeks including shipping.", "", ORIGINAL, "", AI_NOTE]
    return "\n".join(lines)


class Jerseys:
    """The jersey line's work for one worker pass, with Printify, texting and the clock injectable for tests."""

    def __init__(self, shop, *, text_file: Callable[[Path, str], None] = None, text: Callable[[str], None] = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 state_path: Path = STATE, inbox: Path = INBOX, designs_dir: Path = DESIGNS,
                 auto: Optional[Callable[[], bool]] = None):
        from core import phone_alerts
        self.shop, self.c = shop, shop.c
        self.text_file = text_file or (lambda path, note: phone_alerts.send_file(path, note))
        self.text = text or (lambda msg: phone_alerts.send_text(msg))
        self.log, self.clock = log, clock
        self.state_path, self.inbox, self.designs_dir = state_path, inbox, designs_dir
        self._variants: dict = {}
        self._facts: dict = {}
        self._auto = auto

    # -- Printify helpers -----------------------------------------------------------------------------------------
    def variants(self, kind: str) -> list:
        if kind not in self._variants:
            k = KINDS[kind]
            self._variants[kind] = self.c.printify(
                "GET", f"catalog/blueprints/{k['blueprint']}/print_providers/{k['provider']}/variants.json")["variants"]
        return self._variants[kind]

    def facts(self, kind: str) -> list:
        """The maker's own facts (fabric, fit, care) from Printify's catalog, as short lines."""
        if kind not in self._facts:
            try:
                bp = self.c.printify("GET", f"catalog/blueprints/{KINDS[kind]['blueprint']}.json") or {}
                text = re.sub(r"<[^>]+>", "\n", bp.get("description") or "")
                self._facts[kind] = [re.sub(r"^[.:\s]+", "", x).strip() for x in text.split("\n")
                                     if x.strip().startswith(".:")][:6]
            except Exception:
                self._facts[kind] = []
        return self._facts[kind]

    def upload(self, img, name: str) -> str:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        return self.c.printify("POST", "uploads/images.json",
                               {"file_name": name, "contents": base64.b64encode(buf.getvalue()).decode()})["id"]

    def create(self, rec: dict, channel: str, store: dict) -> dict:
        """A product for this jersey in one store, priced from Printify's cost; not published."""
        from worker.shop import price_for
        k = KINDS[rec["kind"]]
        ids = [v["id"] for v in self.variants(rec["kind"])]
        holders = [{"position": pos, "images": [{"id": rec["uploads"][pos], "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}
                   for pos in k["areas"]]
        body = {"title": titles(rec)[channel], "description": description(rec, self.facts(rec["kind"])),
                "tags": TAGS[rec["kind"]], "blueprint_id": k["blueprint"], "print_provider_id": k["provider"],
                "variants": [{"id": i, "price": 4499, "is_enabled": True} for i in ids],
                "print_areas": [{"variant_ids": ids, "placeholders": holders}]}
        made = self.c.printify("POST", f"shops/{store['id']}/products.json", body)
        cost = {v["id"]: int(v.get("cost") or 0) for v in made.get("variants") or []}
        priced = [{"id": i, "price": price_for(cost[i], k["profit"]), "is_enabled": True} for i in ids if cost.get(i)]
        if priced:
            self.c.printify("PUT", f"shops/{store['id']}/products/{made['id']}.json", {"variants": priced})
        return {"id": made["id"], "shop": store["id"], "price": min((p["price"] for p in priced), default=4499),
                "published": False, "images": [i["src"] for i in made.get("images") or []]}

    def retitle(self, path: Path = STATE) -> int:
        """Give every jersey product on every store today's titles() and republish the live ones so the marketplaces
        follow. Returns how many products were updated."""
        st = load(path)
        n = 0
        for rec in st["designs"]:
            t = titles(rec)
            prods = [(ch, (p or {}).get("jersey")) for ch, p in (rec.get("channels") or {}).items()]
            prods.append(("etsy", (rec.get("products") or {}).get("jersey")))
            for ch, prod in prods:
                if not prod or ch not in t:
                    continue
                try:
                    self.c.printify("PUT", f"shops/{prod['shop']}/products/{prod['id']}.json", {"title": t[ch]})
                    if prod.get("published"):
                        self.shop.publish(dict(prod))
                    n += 1
                    self.log(f"jerseys: {rec['slug']} on {ch} is now '{t[ch]}'")
                except Exception as exc:
                    self.log(f"jerseys: couldn't retitle {rec['slug']} on {ch}: {exc}")
        return n

    def publish(self, prod: dict) -> None:
        self.shop.publish(prod)
        prod["listed_at"] = self.clock()

    # -- 1. the inbox -------------------------------------------------------------------------------------------------
    def intake(self, st: dict, stores: dict) -> list:
        """New concept folders in the inbox: art fitted and uploaded, a Pop-Up draft made, the user texted."""
        from PIL import Image
        done = []
        if not self.inbox.is_dir():
            return done
        for folder in sorted(p for p in self.inbox.iterdir() if p.is_dir()):
            front, back = _art_file(folder, "front"), _art_file(folder, "back")
            if not (front and back):
                continue
            slug = slug_of(folder.name)
            if find(st, slug):
                slug = f"{slug}-{int(self.clock())}"
            info = read_info(folder)
            sleeve = _art_file(folder, "sleeve")
            kind = "soccer" if sleeve or info.get("kind") == "soccer" else "basketball"
            if kind == "soccer" and not sleeve:
                sleeve = back                            # no sleeve art: the back's pattern goes on the sleeves
            dest = self.designs_dir / slug
            dest.mkdir(parents=True, exist_ok=True)
            rec = {"id": f"jersey-{slug}", "slug": slug, "concept": folder.name.replace("-", " ").replace("_", " ").strip(),
                   "kind": kind, "made": self.clock(), "status": "new", "uploads": {}, **info}
            try:
                area = {p["position"]: (p["width"], p["height"]) for p in self.variants(kind)[0]["placeholders"]}
                src = {"front": front, "back": back, "sleeve": sleeve}
                for pos, which in KINDS[kind]["areas"].items():
                    with Image.open(src[which]) as im:
                        art = fit(im, area[pos], PANEL_INSET if pos in ("front", "back") else 1.0)
                    if pos == "left_sleeve":
                        art = art.transpose(Image.FLIP_LEFT_RIGHT)
                    if pos in ("front", "back"):
                        art.save(dest / f"{pos}.jpg", "JPEG", quality=92)
                    rec["uploads"][pos] = self.upload(art, f"{slug}-{pos}.jpg")
                for f in folder.iterdir():
                    shutil.copy2(f, dest / f"original-{f.name}")
                rec["file"] = str(dest / "front.jpg")
                rec["title"] = info.get("title") or ""
                rec["channels"] = {"popup": {"jersey": self.create(rec, "popup", stores["popup"])}}
                rec["preview"] = str(self.preview(rec, dest))
                rec["status"] = "approved" if self.auto_approve() else "awaiting"
            except Exception as exc:
                self.log(f"jerseys: couldn't prepare {folder.name}: {exc}")
                rec["status"] = "broken"
                rec["error"] = str(exc)[:300]
            shutil.rmtree(folder, ignore_errors=True)
            st["designs"].append(rec)
            save(st, self.state_path)
            if rec["status"] == "approved":
                self.log(f"jerseys: {slug} approved on its own (auto_approve is on)")
            elif rec["status"] == "awaiting":
                note = (f"🏀 New jersey ready: {rec['concept'].upper()} ({KINDS[kind]['name'].lower()}, "
                        f"${rec['channels']['popup']['jersey']['price'] / 100:.2f} + shipping). Front and back below. "
                        f"To post it, tell Claude: approve jersey {slug}  (or: reject jersey {slug})")
                try:
                    self.text_file(Path(rec["preview"]), note)
                except Exception as exc:
                    self.log(f"jerseys: couldn't text the preview: {exc}")
            else:
                self._say(f"⚠️ Couldn't prepare the jersey '{folder.name}': {rec['error'][:150]}")
            done.append(rec)
        return done

    def refit(self, rec: dict) -> int:
        """Fit a jersey's original art again (as fit() does today) and send it to every product it has, republishing the
        live ones so their photos follow. Returns how many products were updated."""
        from PIL import Image
        dest = self.designs_dir / rec["slug"]
        k = KINDS[rec["kind"]]
        area = {p["position"]: (p["width"], p["height"]) for p in self.variants(rec["kind"])[0]["placeholders"]}
        src = {w: _art_file(dest, f"original-{w}") for w in ("front", "back", "sleeve")}
        src["sleeve"] = src["sleeve"] or src["back"]
        for pos, which in k["areas"].items():
            with Image.open(src[which]) as im:
                art = fit(im, area[pos], PANEL_INSET if pos in ("front", "back") else 1.0)
            if pos == "left_sleeve":
                art = art.transpose(Image.FLIP_LEFT_RIGHT)
            if pos in ("front", "back"):
                art.save(dest / f"{pos}.jpg", "JPEG", quality=92)
            rec["uploads"][pos] = self.upload(art, f"{rec['slug']}-{pos}.jpg")
        ids = [v["id"] for v in self.variants(rec["kind"])]
        holders = [{"position": pos, "images": [{"id": rec["uploads"][pos], "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}
                   for pos in k["areas"]]
        prods = [p.get("jersey") for p in (rec.get("channels") or {}).values()] + [(rec.get("products") or {}).get("jersey")]
        n = 0
        for prod in [p for p in prods if p]:
            made = self.c.printify("PUT", f"shops/{prod['shop']}/products/{prod['id']}.json",
                                   {"print_areas": [{"variant_ids": ids, "placeholders": holders}]}) or {}
            if made.get("images"):
                prod["images"] = [i["src"] for i in made["images"]]
            if prod.get("published"):
                self.shop.publish(prod)
            n += 1
        return n

    def preview(self, rec: dict, dest: Path) -> Path:
        """One picture for the text: the jersey on a person front and back, and flat front and back."""
        import requests
        from PIL import Image
        imgs = rec["channels"]["popup"]["jersey"].get("images") or []
        by = {}
        for src in imgs:
            m = re.search(r"camera_label=([\w-]+)", src)
            if m:
                by.setdefault(m.group(1), src)
        want = [next((k for k in by if "person" in k and k.endswith("front")), None),
                next((k for k in by if "person" in k and k.endswith("back")), None), "front", "back"]
        tiles = []
        for label in want:
            if not label or label not in by:
                continue
            try:
                r = requests.get(by[label], headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
                tiles.append(Image.open(io.BytesIO(r.content)).convert("RGB").resize((800, 800)))
            except Exception:
                continue
        if not tiles:
            for pos in ("front", "back"):
                with Image.open(dest / f"{pos}.jpg") as im:
                    im.thumbnail((800, 800))
                    tiles.append(im.convert("RGB"))
        cols = 2
        sheet = Image.new("RGB", (800 * cols, 800 * ((len(tiles) + 1) // cols)), "white")
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * 800 + (800 - t.width) // 2, (i // cols) * 800 + (800 - t.height) // 2))
        out = dest / f"{rec['slug']}-preview.jpg"
        sheet.save(out, "JPEG", quality=88)
        return out

    # -- 2. approvals -------------------------------------------------------------------------------------------------
    def make_room_tiktok(self, store: dict) -> bool:
        """At TikTok's listing limit: delete the TikTok product of the oldest never-sold daily design (not a winner's
        spin-off). True when there's room."""
        from worker.shop import load_state, save_state
        live = self.shop.tiktok_live(store["id"])
        if live is None or live < TIKTOK_MAX:
            return live is not None
        sst = load_state(self.shop.state_path)
        sold = {i for x in sst.get("sales_log") or [] for i in x.get("designs") or []}
        for d in sst["designs"]:
            if d.get("id") in sold or d.get("spinoff_of"):
                continue
            tiktok = d.get("channels", {}).get("tiktok", {})
            for kind, prod in list(tiktok.items()):
                if not prod.get("published"):
                    continue
                self.c.printify("DELETE", f"shops/{store['id']}/products/{prod['id']}.json")
                del tiktok[kind]
                d.setdefault("tiktok_ended", []).append(kind)
                save_state(sst, self.shop.state_path)
                self.log(f"jerseys: took '{d.get('slogan')}' {kind} off TikTok to make room")
                return True
        return False

    def make_room_ebay(self, store: dict) -> bool:
        from worker.shop import load_state, save_state
        sst = load_state(self.shop.state_path)
        cfg = self.shop.cfg()
        on_ebay = sum(len(d.get("channels", {}).get("ebay", {})) for d in sst["designs"])
        if on_ebay + self.ebay_jerseys() < cfg.get("ebay_max", 250):
            return True
        sold = {i for x in sst.get("sales_log") or [] for i in x.get("designs") or []}
        ok = self.shop.end_oldest_ebay(sst, store, sold)
        save_state(sst, self.shop.state_path)
        return ok

    def ebay_jerseys(self) -> int:
        return sum(1 for r in load(self.state_path)["designs"] if (r.get("channels", {}).get("ebay") or {}).get("jersey"))

    def list_approved(self, st: dict, stores: dict) -> list:
        """Approved jerseys onto TikTok, eBay and the Pop-Up Store (Etsy waits its turn). Returns the lines listed."""
        out = []
        for rec in st["designs"]:
            if rec.get("status") != "approved":
                continue
            listed, failed = [], []
            for channel, room in (("tiktok", self.make_room_tiktok), ("ebay", self.make_room_ebay), ("popup", None)):
                store = stores.get(channel)
                if not store:
                    continue
                prods = rec.setdefault("channels", {}).setdefault(channel, {})
                prod = prods.get("jersey")
                if prod and prod.get("published"):
                    continue
                try:
                    if not prod and room and not room(store):
                        failed.append(f"{channel}: no room")
                        continue
                    prod = prods["jersey"] = prod or self.create(rec, channel, store)
                    save(st, self.state_path)
                    self.publish(prod)
                    listed.append(channel)
                except Exception as exc:
                    failed.append(f"{channel}: {str(exc)[:120]}")
                    self.log(f"jerseys: {rec['slug']} on {channel} failed: {exc}")
            rec["status"] = "listed" if not failed else "approved"
            rec.setdefault("errors", []).extend(failed)
            if len(rec["errors"]) > 9:
                rec["status"] = "listed"             # stop retrying a channel that keeps failing; the rest are up
            rec["etsy_queued"] = True
            save(st, self.state_path)
            names = {"tiktok": "TikTok Shop", "ebay": "eBay", "popup": "our shop + Pinterest"}
            if listed:
                out.append(f"{rec['concept'].upper()} on " + ", ".join(names[c] for c in listed))
            if failed and rec["status"] == "listed":
                self._say(f"⚠️ {rec['concept'].upper()} couldn't go on: {'; '.join(failed)[:200]}")
        return out

    def etsy_turn(self, st: dict, stores: dict) -> Optional[str]:
        """One approved jersey onto Etsy every ETSY_EVERY_DAYS ($0.20 a listing), oldest approval first."""
        if "etsy" not in stores:
            return None
        last = max((r["products"]["jersey"].get("listed_at", 0) for r in st["designs"]
                    if (r.get("products") or {}).get("jersey")), default=0)
        if self.clock() - last < ETSY_EVERY_DAYS * 86400:
            return None
        rec = next((r for r in st["designs"] if r.get("etsy_queued") and not (r.get("products") or {}).get("jersey")), None)
        if not rec:
            return None
        try:
            prod = rec.setdefault("products", {})["jersey"] = self.create(rec, "etsy", stores["etsy"])
            save(st, self.state_path)
            self.publish(prod)
            save(st, self.state_path)
            return f"{rec['concept'].upper()} on Etsy"
        except Exception as exc:
            rec["etsy_queued"] = False
            save(st, self.state_path)
            self.log(f"jerseys: {rec['slug']} on Etsy failed: {exc}")
            return None

    def reject(self, st: dict) -> None:
        for rec in st["designs"]:
            if rec.get("status") != "rejecting":
                continue
            for channel, prods in (rec.get("channels") or {}).items():
                prod = prods.get("jersey")
                if prod and not prod.get("published"):
                    try:
                        self.c.printify("DELETE", f"shops/{prod['shop']}/products/{prod['id']}.json")
                    except Exception as exc:
                        self.log(f"jerseys: couldn't delete the {channel} draft of {rec['slug']}: {exc}")
                    prods.pop("jersey", None)
            rec["status"] = "rejected"
            save(st, self.state_path)

    def auto_approve(self) -> bool:
        """The user's standing yes (2026-11-19, "when I wake up they should be up and listed"): list without asking."""
        if self._auto is not None:
            return self._auto()
        try:
            from worker.jersey_studio import settings
            return bool(settings().get("auto_approve"))
        except Exception:
            return False

    def _say(self, msg: str) -> None:
        from datetime import datetime
        hour = datetime.fromtimestamp(self.clock()).hour
        if hour >= 22 or hour < 8:                      # never buzz the phone at night; the log keeps it
            self.log(f"jerseys (not texted at night): {msg}")
            return
        try:
            self.text(msg)
        except Exception as exc:
            self.log(f"jerseys: couldn't text: {exc}")

    def run(self) -> bool:
        """One pass. True when something new went live (so the storefront and its Pinterest feed rebuild)."""
        st = load(self.state_path)
        has_inbox = self.inbox.is_dir() and any(p.is_dir() for p in self.inbox.iterdir())
        busy = any(r.get("status") in ("approved", "rejecting") or r.get("etsy_queued") for r in st["designs"])
        if not (has_inbox or busy):
            return False
        stores = self.shop.stores()
        self.intake(st, stores)
        self.reject(st)
        live = self.list_approved(st, stores)
        etsy = self.etsy_turn(st, stores)
        if etsy:
            live.append(etsy)
        if live:
            self._say("✅ Jersey live: " + "; ".join(live) + ".")
        return bool(live)


def pin_recs(path: Path = STATE) -> list:
    """Listed jerseys shaped like shop designs, for the storefront (whose feed Pinterest pins from)."""
    out = []
    for rec in load(path)["designs"]:
        if rec.get("status") != "listed":
            continue
        name = titles(rec)["popup"]
        out.append({"id": rec["id"], "title": name, "slogan": rec["concept"].upper(), "made": rec["made"],
                    "description": f"{rec['concept'].upper()}: a vintage Y2K streetwear {KINDS[rec['kind']]['name'].lower()}, "
                                   "printed all over. " + ORIGINAL,
                    "file": rec.get("file", ""), "products": dict(rec.get("products") or {}),
                    "channels": {ch: {("jersey" if rec["kind"] == "basketball" else "soccer_jersey"): p
                                      for p in prods.values()} for ch, prods in (rec.get("channels") or {}).items()}})
        if rec["kind"] == "soccer" and out[-1]["products"].get("jersey"):
            out[-1]["products"] = {"soccer_jersey": out[-1]["products"]["jersey"]}
    return out


def mark(name: str, status: str, path: Path = STATE) -> str:
    """The user's yes or no (from their phone, through Claude). The worker does the listing on its next pass."""
    st = load(path)
    if name in ("all", "everything"):
        recs = [r for r in st["designs"] if r.get("status") == "awaiting"]
    else:
        rec = find(st, name)
        recs = [rec] if rec else []
    if not recs:
        waiting = ", ".join(r["slug"] for r in st["designs"] if r.get("status") == "awaiting") or "none"
        return f"No jersey called '{name}' is waiting. Waiting: {waiting}."
    for rec in recs:
        if rec.get("status") != "awaiting":
            return f"{rec['slug']} is already {rec.get('status')}."
        rec["status"] = status
    save(st, path)
    verb = "will go live on TikTok Shop, eBay, our shop and Pinterest within about 5 minutes (Etsy in its turn)" \
        if status == "approved" else "will be deleted"
    return f"{', '.join(r['slug'] for r in recs)} {verb}."


def summary(path: Path = STATE) -> str:
    st = load(path)
    if not st["designs"]:
        return "No jerseys yet. Drop art in memory/jersey_art/inbox/<concept>/ (front.png, back.png)."
    return "\n".join(f"{r['slug']}: {r.get('status')} ({r['kind']})"
                     + (" · on " + ", ".join(ch for ch, p in (r.get('channels') or {}).items() if (p.get('jersey') or {}).get('published')) if r.get("channels") else "")
                     + (" · Etsy" if (r.get("products") or {}).get("jersey") else "")
                     for r in st["designs"])


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] in ("approve", "reject"):
        print(mark(" ".join(args[1:]), "approved" if args[0] == "approve" else "rejecting"))
    elif args[:1] == ["retitle"]:
        from worker.shop import Shop
        print(Jerseys(Shop()).retitle(), "jersey products retitled")
    else:
        print(summary())
