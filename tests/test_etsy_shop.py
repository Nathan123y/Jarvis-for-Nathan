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

    def idea(self, recent, trending="", notes=""):
        self.trending, self.notes = trending, notes
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
        for bp, pos, w, h, opts in ((77, "front", 2919, 1944, {"color": "Sport Grey", "size": "M"}),
                                    (39, "front", 3210, 3852, {"color": "White", "size": "L"}),
                                    (1574, "front_left_leg", 2100, 2400, {"color": "Ivory", "size": "S"}),
                                    (1588, "left_leg_front", 1048, 3130, {"color": "Ivory", "size": "XL"}),
                                    (1128, "front", 1650, 750, {"color": "Black", "size": "One size"})):
            if path.startswith(f"catalog/blueprints/{bp}/"):
                return {"variants": [{"id": bp * 10, "title": f"{opts['color']} / {opts['size']}", "options": opts,
                                      "placeholders": [{"position": pos, "width": w, "height": h}]}]}
        if path == "uploads/images.json":
            return {"id": f"img{len([c for c in self.calls if c[1] == 'uploads/images.json'])}"}
        if method == "POST" and path.endswith("/products.json"):
            self.next_id += 1
            costs = {12: 1129, 478: 503, 400: 158, 77: 2150, 39: 1050, 1574: 2290, 1588: 2340, 1128: 1490}
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
        fetch = cfg.pop("trends_fetch", None)
        settings = dict(ws.DEFAULTS, **{"products": ["tee", "mug", "sticker"], **cfg})   # the original three, unless a test says
        return ws.Shop(clients, state_path=self.dir / "s.json", art_dir=self.dir / "art", cfg=lambda: settings,
                       log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)), clock=lambda: 1_792_900_000,
                       trends_fetch=fetch or (lambda: {}), notebook_path=self.dir / "notebook.json")


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

    def test_the_full_lineup_hoodie_tank_shorts_pants_hat_and_no_sticker(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c, products=list(ws.DEFAULTS["products"]))
        s.make_design()
        done = s.list_pending()
        self.assertEqual(len(done), 7)
        created = {b["blueprint_id"]: b for m, path, b in c.calls if m == "POST" and path == "shops/9/products.json"}
        self.assertEqual(sorted(created), [12, 39, 77, 478, 1128, 1574, 1588])            # no sticker (400)
        pos = lambda bp: created[bp]["print_areas"][0]["placeholders"][0]
        self.assertEqual(pos(1574)["position"], "front_left_leg")
        self.assertEqual(pos(1588)["position"], "left_leg_front")
        self.assertEqual(pos(1588)["images"][0]["y"], 0.3)                               # up on the thigh
        hat_img = pos(1128)["images"][0]["id"]
        self.assertNotEqual(hat_img, pos(12)["images"][0]["id"])                         # the hat gets the slogan-only art
        self.assertTrue(list((self.dir / "art").glob("*-slogan.png")))

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
        self.assertEqual(len([t for k, t in self.sent if "Couldn't list" in t and "eBay" in t]), 1)

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

    def test_designs_riff_on_todays_trends_fetched_once_a_day(self):
        fetched = []
        c = FakeClients()
        s = self.shop(c, trends_fetch=lambda: fetched.append(1) or {"Know Your Meme (new memes)": ["Black Cat Clapping"]})
        s.make_design()
        s.make_design()
        self.assertEqual(len(fetched), 1)
        self.assertIn("Black Cat Clapping", c.trending)

    def test_feed_titles(self):
        from worker import trends
        xml = ("<rss><channel><title>Feed</title><item><title>What's up with &quot;6 7&quot;?</title></item>"
               "<item><title><![CDATA[Black Cat Clapping]]></title></item><item><title>me_irl</title></item></channel></rss>")
        self.assertEqual(trends.titles(xml), ['"6 7"?', "Black Cat Clapping"])

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
        c.orders.append({"id": "o2", "total_price": 1500,                              # Printify's cost, not the sale
                         "line_items": [{"metadata": {"title": "Plant Lover T-Shirt"}, "product_id": "tee-1", "quantity": 1}]})
        st = ws.load_state(self.dir / "s.json")
        st["designs"].append({"id": "d", "made": 0, "slogan": "PLANT LOVER", "niche": "plants", "only": ["tee"],
                              "products": {"tee": {"id": "tee-1", "price": 2899, "published": True}}})
        ws.save_state(st, self.dir / "s.json")
        lines = s.new_orders()
        self.assertEqual(lines[0], "💰 SALE on Etsy! Plant Lover T-Shirt ($28.99). Printify will print and ship it.")
        self.assertIn('🏆 First sale for "PLANT LOVER"', lines[1])
        st = ws.load_state(self.dir / "s.json")
        self.assertEqual(st["make_spinoff"], ["d", "d"])                              # two more on the winning theme
        self.assertNotIn("only", st["designs"][-1])                                    # and every product, not just the tee
        night = ws.overnight(0, self.dir / "s.json")
        self.assertEqual((night["sales"], night["revenue"], night["listed"]), (1, 28.99, 1))
        self.assertEqual(s.new_orders(), [])
        c.orders[1]["status"] = "in-production"
        self.assertEqual(s.new_orders(), ["📦 Your Etsy order (Plant Lover T-Shirt) is being printed."])
        c.orders[1].update(status="fulfilled", shipments=[{"carrier": "usps", "number": "9400", "url": "https://t.co/9400"}])
        self.assertEqual(s.new_orders(), ["📦 Your Etsy order (Plant Lover T-Shirt) has shipped: USPS https://t.co/9400."])
        c.orders[0]["status"] = "canceled"                                              # baseline orders update too
        self.assertEqual(s.new_orders(), ["📦 Your Etsy order (Old order) was canceled."])


class ScheduleTests(_Base):
    def test_one_design_a_day_after_the_set_time_and_make_now(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)))
        with patch.object(ws, "settings", lambda: dict(ws.DEFAULTS)), patch("worker.storefront.publish", lambda *a, **k: ""):
            svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=5))
            self.assertEqual(len(ws.load_state(self.dir / "s.json")["designs"]), 0)       # before 6:00
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

    def test_make_now_queue_makes_one_per_pass_on_only_the_asked_products(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)))
        st = ws.load_state(self.dir / "s.json")
        st.update(make_now=2, make_only=["tee"])
        ws.save_state(st, self.dir / "s.json")
        with patch.object(ws, "settings", lambda: dict(ws.DEFAULTS, designs_per_day=0)), \
                patch("worker.storefront.publish", lambda *a, **k: ""):
            for hour in (13, 14, 15):
                svc.run_once(s, now=datetime.fromtimestamp(1_792_900_000).replace(hour=hour))
        st = ws.load_state(self.dir / "s.json")
        self.assertEqual(len(st["designs"]), 2)
        self.assertEqual(st["make_now"], 0)
        self.assertNotIn("make_only", st)
        created = [x for x in c.calls if x[0] == "POST" and x[1] == "shops/9/products.json"]
        self.assertEqual({b["blueprint_id"] for _, _, b in created}, {12})                # t-shirts only


if __name__ == "__main__":
    unittest.main()


class StorefrontTests(_Base):
    def test_site_has_a_page_per_listed_design_buy_links_and_a_feed(self):
        from worker import storefront as sf
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        s.make_design()
        s.list_pending()
        real = c.printify

        def printify(m, p, b=None):
            if m == "GET" and p.startswith("shops/9/products/"):
                pid = p.split("/")[3].split(".")[0]
                return {"external": {"handle": f"https://www.etsy.com/listing/{pid}"},
                        "images": [{"src": f"https://img/{pid}?camera_label=front"}]}
            return real(m, p, b)
        designs = ws.load_state(self.dir / "s.json")["designs"]
        out = self.dir / "site"
        n = sf.build(designs, printify, out, {**sf.DEFAULTS, "pinterest_tag": "abc123"},
                     download=lambda url, dest: dest.write_bytes(b"jpg") or True)
        self.assertEqual(n, 1)
        page = (out / "p" / f"{designs[0]['id']}.html").read_text()
        self.assertIn("https://www.etsy.com/listing/p1", page)
        self.assertIn("T-Shirt on Etsy", page)
        self.assertIn('name="p:domain_verify" content="abc123"', page)
        feed = (out / "feed.xml").read_text()
        self.assertIn(f"https://nathan123y.github.io/p/{designs[0]['id']}.html", feed)
        self.assertIn("<enclosure", feed)

    def test_site_url_for_user_and_project_pages(self):
        from worker import storefront as sf
        self.assertEqual(sf.site_url("Nathan123y/Nathan123y.github.io"), "https://nathan123y.github.io/")
        self.assertEqual(sf.site_url("Nathan123y/shop"), "https://nathan123y.github.io/shop/")


class EtsyPhotoTests(_Base):
    def test_extra_photos_go_to_each_etsy_listing_once(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        s.make_design()
        s.list_pending()
        st = ws.load_state(self.dir / "s.json")
        for prod in st["designs"][0]["products"].values():
            prod["url"] = f"https://www.etsy.com/listing/{prod['id'][1:]}00/funny"
            self.assertTrue(any(p.endswith("-design.jpg") for p in prod["extra_photos"]))   # close-up saved
        ws.save_state(st, self.dir / "s.json")
        uploads = []
        svc = ws.ShopService(log=lambda m: None)
        with patch("worker.etsy_api.connected", lambda: True), \
                patch("worker.etsy_api.add_photos", lambda listing, paths: uploads.append(listing) or len(paths)):
            svc.etsy_photos(s)
            svc.etsy_photos(s)
        self.assertEqual(sorted(uploads), ["100", "200", "300"])

    def test_listing_id_and_a_stale_approval_is_refused(self):
        from worker import etsy_api
        self.assertEqual(etsy_api.listing_id("https://www.etsy.com/listing/4602580828/funny"), "4602580828")
        with patch("worker.etsy_api._cfg", lambda: {"keystring": "k", "pkce_state": "new", "pkce_verifier": "v"}):
            with self.assertRaises(RuntimeError):
                etsy_api.finish("https://localhost:3003/callback?code=abc&state=old")
            with self.assertRaises(RuntimeError):
                etsy_api.finish("https://localhost:3003/callback?state=new")


class VideoTests(_Base):
    def _listed_shop(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        s.make_design()
        s.list_pending()
        return s

    def test_three_videos_a_day_one_per_time_each_a_different_format(self):
        s = self._listed_shop()
        made, texted = [], []
        svc = ws.ShopService(log=lambda m: None)

        def fake_make(rec, fmt, out_dir, name, ask=None, printify=None):
            rec["video"] = {"hook": "h"}
            made.append(fmt)
            return {"file": str(self.dir / f"{fmt}.mp4"), "caption": "c #tag", "format": fmt, "design": rec["id"]}
        day = datetime(2026, 10, 25)
        with patch("worker.videos.make", fake_make), patch("core.phone_alerts.send_file", lambda path, note, cfg=None: texted.append((path, note))), \
                patch("core.phone_alerts.settings", lambda: {"enabled": True, "off": []}), \
                patch.object(ws, "settings", lambda: dict(ws.DEFAULTS, video_times="11:00,15:00,19:00")):
            self.assertEqual(svc.video_slot(s, day.replace(hour=10)), "")              # before the first time
            self.assertEqual(svc.video_slot(s, day.replace(hour=11, minute=5)), "texted")
            self.assertEqual(svc.video_slot(s, day.replace(hour=12)), "")              # that slot is done
            svc.video_slot(s, day.replace(hour=15, minute=1))
            svc.video_slot(s, day.replace(hour=19, minute=30))
            svc.video_slot(s, day.replace(hour=22))
        self.assertEqual(made, ["reveal", "pov", "gifts"])
        self.assertEqual(len(texted), 3)
        self.assertIn("Video 1 of 3", texted[0][1])
        self.assertIn("#tag", texted[0][1])
        self.assertEqual(len(ws.load_state(self.dir / "s.json")["videos"]), 3)

    def test_videos_are_off_by_default(self):
        s = self._listed_shop()
        with patch.object(ws, "settings", lambda: dict(ws.DEFAULTS)), \
                patch("worker.videos.make", lambda *a, **k: self.fail("made a video")):
            self.assertEqual(ws.ShopService(log=lambda m: None).video_slot(s, datetime(2026, 10, 25, 20)), "")

    def test_a_late_start_makes_one_video_not_a_backlog(self):
        s = self._listed_shop()
        made = []
        svc = ws.ShopService(log=lambda m: None)
        with patch("worker.videos.make", lambda rec, fmt, *a, **k: made.append(fmt) or
                   {"file": "f.mp4", "caption": "c", "format": fmt, "design": rec["id"]}), \
                patch("core.phone_alerts.send_file", lambda *a, **k: None), \
                patch("core.phone_alerts.settings", lambda: {"enabled": True, "off": []}), \
                patch.object(ws, "settings", lambda: dict(ws.DEFAULTS, video_times="11:00,15:00,19:00")):
            svc.video_slot(s, datetime(2026, 10, 25, 20, 0))                          # Mac woke at 8 PM
            svc.video_slot(s, datetime(2026, 10, 25, 20, 10))
        self.assertEqual(made, ["reveal"])

    def test_music_and_copy_fallback(self):
        from worker import videos
        path = self.dir / "m.wav"
        videos.music(path, 3.0, seed=1)
        self.assertGreater(path.stat().st_size, 3 * 44100)
        copy = videos.video_copy({"title": "Plant Gift", "slogan": "I PROBABLY OVERWATERED IT", "niche": "plant lovers"},
                                 ask=lambda p: {"hook": "Disney plants", "pov": "POV: you water daily",
                                                "caption": "Cute. Link in bio.", "hashtags": ["#Plant Mom", "gift"]})
        self.assertEqual(copy["hook"], "Made for plant lovers")                       # protected name refused
        self.assertEqual(copy["pov"], "POV: you water daily")
        self.assertEqual(copy["hashtags"], ["plantmom", "gift"])
