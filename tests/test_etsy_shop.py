"""The Etsy print-on-demand shop: ideas, artwork, pricing, listing through Printify, the daily schedule."""
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from worker import shop as ws

IDEA = {"niche": "houseplant lovers", "image_prompt": "a shy cartoon houseplant, flat vector, white background",
        "slogan": "I promise I have enough plants", "title": "Funny Plant Lover Gift Houseplant Addict",
        "description": "For the friend whose windowsill is a jungle.",
        "tags": ["plant lover gift", "Houseplant!", "plant lover gift", "a tag that is far too long to fit"] + [f"tag {i}" for i in range(12)]}


def _png() -> bytes:
    img = Image.new("RGB", (256, 256), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((60, 60, 196, 196), fill=(40, 160, 60), outline=(20, 20, 20), width=6)
    d.ellipse((110, 110, 146, 146), fill="white")                  # white inside the drawing must stay
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeClients:
    printify_token, cf_token = "t", "c"

    def __init__(self, shops=None, idea=IDEA):
        self.shops = shops if shops is not None else []
        self.ideas, self.calls, self.next_id = [idea], [], 0
        self.orders = []

    def idea(self, recent):
        return self.ideas.pop(0) if len(self.ideas) > 1 else self.ideas[0]

    def draw(self, prompt):
        return _png()

    def printify(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "shops.json":
            return self.shops
        if path.startswith("catalog/blueprints/12/"):
            return {"variants": [{"id": 1, "title": "White / S", "options": {"color": "White", "size": "S"},
                                  "placeholders": [{"position": "front", "width": 2767, "height": 3362}]},
                                 {"id": 2, "title": "White / XS", "options": {"color": "White", "size": "XS"},
                                  "placeholders": [{"position": "front", "width": 2767, "height": 3362}]},
                                 {"id": 3, "title": "Black / M", "options": {"color": "Black", "size": "M"},
                                  "placeholders": [{"position": "front", "width": 2767, "height": 3362}]}]}
        if path.startswith("catalog/blueprints/478/"):
            return {"variants": [{"id": 10, "title": "11oz", "options": {"size": "11oz"},
                                  "placeholders": [{"position": "front", "width": 2475, "height": 1155}]},
                                 {"id": 11, "title": "15oz", "options": {"size": "15oz"},
                                  "placeholders": [{"position": "front", "width": 2475, "height": 1155}]}]}
        if path.startswith("catalog/blueprints/400/"):
            return {"variants": [{"id": 20, "title": '3" × 3" / White', "options": {"size": '3" × 3"'},
                                  "placeholders": [{"position": "front", "width": 559, "height": 559}]}]}
        if path == "uploads/images.json":
            return {"id": "img1"}
        if method == "POST" and path.endswith("/products.json"):
            self.next_id += 1
            costs = {12: 1129, 478: 503, 400: 158}
            return {"id": f"p{self.next_id}", "variants": [{"id": v["id"], "cost": costs[body["blueprint_id"]]}
                                                          for v in body["variants"]]}
        if "/orders.json" in path:
            return {"data": self.orders}
        return {}


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sent = []

    def shop(self, clients, **cfg):
        settings = dict(ws.DEFAULTS, **cfg)
        return ws.Shop(clients, state_path=self.dir / "s.json", art_dir=self.dir / "art", cfg=lambda: settings,
                       log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)), clock=lambda: 1_792_900_000)


class IdeaAndArtTests(unittest.TestCase):
    def test_etsy_limits_and_slogan_cleanup(self):
        out = ws.clean_idea(IDEA)
        self.assertEqual(len(out["tags"]), 13)
        self.assertTrue(all(len(t) <= 20 for t in out["tags"]))
        self.assertEqual(out["tags"][:2], ["plant lover gift", "houseplant"])            # deduped, cleaned
        self.assertEqual(out["slogan"], "I PROMISE I HAVE ENOUGH PLANTS")

    def test_protected_names_are_refused(self):
        for bad in ("a Pikachu holding coffee", "Taylor Swift fan gift", "NFL dad shirt", "Disney castle mom"):
            with self.assertRaises(ValueError):
                ws.clean_idea({**IDEA, "title": bad})
        ws.clean_idea({**IDEA, "title": "Grandpa Fishing Gift"})                       # 'pa' inside words is fine

    def test_price_leaves_the_profit_after_fees(self):
        cents = ws.price_for(1129, 8.0)
        self.assertEqual(cents % 100, 99)
        self.assertGreaterEqual(cents / 100 * 0.85 - 11.29, 8.0 - 0.01)

    def test_background_goes_but_white_inside_the_drawing_stays(self):
        art = ws.cut_background(Image.open(io.BytesIO(_png())))
        self.assertLess(art.width, 256)                                                # cropped to the drawing
        self.assertEqual(art.getpixel((0, 0))[3], 0)                                   # corner: transparent
        cx, cy = art.width // 2, art.height // 2
        self.assertEqual(art.getpixel((cx, cy)), (255, 255, 255, 255))                  # inner white: kept

    def test_slogan_is_set_under_the_art_at_print_width(self):
        design = ws.compose(ws.cut_background(Image.open(io.BytesIO(_png()))), "I PROMISE I HAVE ENOUGH PLANTS")
        self.assertEqual(design.width, ws.DESIGN_W)
        self.assertGreater(design.height, ws.DESIGN_W)                                  # art plus the slogan below


class ListingTests(_Base):
    def test_designs_wait_until_etsy_is_connected_then_list_once(self):
        c = FakeClients(shops=[{"id": 7, "sales_channel": "disconnected"}])
        s = self.shop(c)
        s.make_design()
        self.assertEqual(s.list_pending(), [])
        self.assertFalse([x for x in c.calls if x[0] == "POST" and x[1].endswith("products.json")])
        c.shops.append({"id": 9, "sales_channel": "etsy"})
        done = s.list_pending()
        self.assertEqual(len(done), 3)
        self.assertIn("T-Shirt on Etsy ($", done[0])
        created = [x for x in c.calls if x[0] == "POST" and x[1] == "shops/9/products.json"]
        self.assertEqual([b["blueprint_id"] for _, _, b in created], [12, 478, 400])
        tee = created[0][2]
        self.assertEqual([v["id"] for v in tee["variants"]], [1])                      # White S only: no XS, no Black
        self.assertTrue(tee["title"].endswith("T-Shirt"))
        self.assertIn("AI", tee["description"])
        self.assertEqual([v["id"] for v in created[1][2]["variants"]], [10])           # the 11oz mug
        mug = created[1][2]["print_areas"][0]["placeholders"][0]["images"]
        self.assertEqual([i["x"] for i in mug], [0.5])                                 # centred: the photo Etsy gets
        prices = [x for x in c.calls if x[0] == "PUT"]
        self.assertEqual(prices[0][2]["variants"][0]["price"], ws.price_for(1129, 8.0))
        self.assertEqual(len([x for x in c.calls if x[1].endswith("publish.json")]), 3)
        self.assertEqual(s.list_pending(), [])                                         # nothing twice
        self.assertEqual(len([x for x in c.calls if x[1].endswith("publish.json")]), 3)

    def test_every_design_goes_to_ebay_too_with_short_titles(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}, {"id": 5, "sales_channel": "ebay"},
                               {"id": 3, "sales_channel": "disconnected"}])
        s = self.shop(c)
        s.make_design()
        done = s.list_pending()
        self.assertEqual(len(done), 6)
        self.assertTrue(any("on eBay" in d for d in done))
        ebay = [b for m, p, b in c.calls if m == "POST" and p == "shops/5/products.json"]
        self.assertEqual(len(ebay), 3)
        self.assertTrue(all(len(b["title"]) <= 80 and b["title"].endswith(("T-Shirt", "Mug", "Sticker")) for b in ebay))
        self.assertFalse([p for m, p, b in c.calls if p.startswith("shops/3/")])           # not a sales channel
        rec = ws.load_state(self.dir / "s.json")["designs"][0]
        self.assertEqual(set(rec["channels"]["ebay"]), {"tee", "mug", "sticker"})

    def test_a_failing_channel_does_not_hold_up_the_other(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}, {"id": 5, "sales_channel": "ebay"}])
        s = self.shop(c)
        s.make_design()
        real = c.printify
        c.printify = lambda m, p, b=None: (_ for _ in ()).throw(RuntimeError("eBay said no")) \
            if p.startswith("shops/5/products/") and p.endswith("publish.json") else real(m, p, b)
        done = s.list_pending()
        self.assertEqual([d for d in done if "Etsy" in d].__len__(), 3)
        self.assertFalse([d for d in done if "eBay" in d])

    def test_listing_title_fits_the_channel(self):
        long = "Funny Overwatered Plant Lover Gift Idea For Garden Fairy Houseplant Mom Birthday Present"
        t = ws.listing_title(long, "T-Shirt", 80)
        self.assertLessEqual(len(t), 80)
        self.assertTrue(t.endswith(" T-Shirt"))
        self.assertEqual(ws.listing_title("Short Title", "Mug", 80), "Short Title Mug")

    def test_a_failed_listing_waits_an_hour(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        s.make_design()
        real = c.printify
        c.printify = lambda m, p, b=None: (_ for _ in ()).throw(RuntimeError("Etsy said no")) if p.endswith("publish.json") else real(m, p, b)
        self.assertEqual(s.list_pending(), [])
        self.assertEqual(s.list_pending(), [])                                         # same hour: not retried
        rec = ws.load_state(self.dir / "s.json")["designs"][0]
        self.assertEqual(len(rec["errors"]), 1)
        c.printify = real
        s.clock = lambda: 1_792_900_000 + 3601
        self.assertEqual(len(s.list_pending()), 3)

    def test_a_refused_idea_is_retried(self):
        c = FakeClients()
        c.ideas = [{**IDEA, "title": "Pokemon trainer gift"}, IDEA]
        rec = self.shop(c).make_design()
        self.assertEqual(rec["title"], IDEA["title"])

    def test_orders_first_look_is_a_baseline(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        c.orders = [{"id": "o1", "line_items": [{"metadata": {"title": "Old order"}}]}]
        self.assertEqual(s.new_orders(), [])
        c.orders.append({"id": "o2", "total_price": 2899, "line_items": [{"metadata": {"title": "Plant Lover T-Shirt"}}]})
        self.assertEqual(s.new_orders(), ["🛍️ New Etsy order: Plant Lover T-Shirt ($28.99). Printify will print and ship it."])
        self.assertEqual(s.new_orders(), [])


class ScheduleTests(_Base):
    def test_one_design_a_day_after_the_set_time_and_make_now(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)))
        with patch.object(ws, "settings", lambda: dict(ws.DEFAULTS)):
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=9))
            self.assertEqual(len(ws.load_state(self.dir / "s.json")["designs"]), 0)       # before 10:00
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=11))
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=12))
            self.assertEqual(len(ws.load_state(self.dir / "s.json")["designs"]), 1)       # once a day
            self.assertEqual(self.sent[0][0], "shop")
            self.assertIn("Just listed", self.sent[0][1])
            st = ws.load_state(self.dir / "s.json")
            st["make_now"] = True
            ws.save_state(st, self.dir / "s.json")
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=13))
            self.assertEqual(len(ws.load_state(self.dir / "s.json")["designs"]), 2)


if __name__ == "__main__":
    unittest.main()
