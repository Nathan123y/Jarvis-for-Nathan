"""The Etsy print-on-demand shop: ideas, artwork, pricing, listing through Printify, the daily schedule."""
import io
import json
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
        if path.startswith("catalog/blueprints/1023/"):
            areas = [{"position": pos, "width": w, "height": h} for pos, w, h in
                     (("front", 2601, 2611), ("back", 2612, 2662), ("right_sleeve", 2023, 2370), ("left_sleeve", 2023, 2370),
                      ("collar", 2267, 228), ("waistband", 5818, 575))]
            return {"variants": [{"id": 1023 * 10 + i, "title": size, "options": {"size": size},
                                  "placeholders": areas} for i, size in enumerate(("M", "3XL"))]}
        if path.startswith("catalog/blueprints/1599/"):
            shapes = (("Circle", 886, 886), ("Heart", 945, 886), ("Oval", 720, 1004), ("Star", 961, 921))
            return {"variants": [{"id": 15990 + i, "title": f"{shape} / One size", "options": {"shape": shape, "size": "One size"},
                                  "placeholders": [{"position": "front", "width": w, "height": h}]}
                                 for i, (shape, w, h) in enumerate(shapes)]}
        if path == "catalog/blueprints/12.json":
            return {"description": "<p>A classic tee.</p><div>.:Material: 100% cotton</div><br /><div>.:Blank product sourced from China</div>"}
        if path.startswith("catalog/blueprints/400/"):
            return {"variants": [{"id": 20, "title": '3" × 3" / White', "options": {"size": '3" × 3"'},
                                  "placeholders": [{"position": "front", "width": 559, "height": 559}]}]}
        for bp, pos, w, h, opts in ((77, "front", 2919, 1944, {"color": "Sport Grey", "size": "M"}),
                                    (39, "front", 3210, 3852, {"color": "White", "size": "L"}),
                                    (1574, "front_left_leg", 2100, 2400, {"color": "Ivory", "size": "S"}),
                                    (1588, "left_leg_front", 1048, 3130, {"color": "Ivory", "size": "XL"}),
                                    (1128, "front", 1650, 750, {"color": "Black", "size": "One size"}),
                                    (1691, "front", 1500, 525, {"color": "Black", "size": "One size"}),
                                    (562, "front", 3150, 1691, {"size": '20" × 10"'}),
                                    (1367, "front", 9075, 10800, {"size": '30" x 36"'})):
            if path.startswith(f"catalog/blueprints/{bp}/"):
                return {"variants": [{"id": bp * 10, "title": " / ".join(opts.values()), "options": opts,
                                      "placeholders": [{"position": pos, "width": w, "height": h}]}]}
        if path == "uploads/images.json":
            return {"id": f"img{len([c for c in self.calls if c[1] == 'uploads/images.json'])}"}
        if method == "POST" and path.endswith("/products.json"):
            self.next_id += 1
            costs = {1023: 3100, 12: 1129, 478: 503, 400: 158, 77: 2150, 39: 1050, 1574: 2290, 1588: 2340, 1128: 1490}
            return {"id": f"p{self.next_id}", "variants": [{"id": v["id"], "cost": costs.get(body["blueprint_id"], 1500)}
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
        settings = dict(ws.DEFAULTS, **{"products": ["tee", "mug", "sticker"],   # the original three, unless a test says
                                        "etsy_per_day": 99, "etsy_holiday_only": False, "etsy_products": [], "etsy_lines": [],
                                        "etsy_per_design": 99, **cfg})
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

    def test_etsy_gets_at_most_etsy_per_design_listings_in_order_of_preference(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c, products=["mug", "tee", "hoodie", "tank"], etsy_products=["tee", "hoodie", "mug"],
                      etsy_per_design=2)
        s.make_design()
        s.list_pending()
        created = [b["blueprint_id"] for m, path, b in c.calls if m == "POST" and path == "shops/9/products.json"]
        self.assertEqual(sorted(created), [12, 77])                                     # tee and hoodie, not the mug

    def test_a_design_already_on_etsy_gets_no_new_etsy_listings(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c, products=["tee", "mug"])
        rec = s.make_design()
        st = ws.load_state(s.state_path)
        st["designs"][0]["products"] = {"tee": {"id": "p1", "shop": 9, "price": 2999, "published": True}}
        ws.save_state(st, s.state_path)
        self.assertEqual(s.list_pending(), [])
        self.assertEqual(ws.load_state(s.state_path)["designs"][0]["etsy_kinds"], ["tee"])

    def test_embroidered_caps_get_the_slogan_in_light_thread_and_wrapping_paper_a_tiled_sheet(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c, products=["emb_beanie", "wrap"])
        s.make_design()
        s.list_pending()
        light = next((self.dir / "art").glob("*-slogan-light.png"))
        with Image.open(light) as im:
            ink = max(im.getcolors(1 << 20), key=lambda x: x[0] if x[1][3] else 0)[1]
        self.assertGreater(sum(ink[:3]), 600)                                           # cream, not dark
        tile = next((self.dir / "art").glob("*-tile-*.jpg"))
        with Image.open(tile) as im:
            self.assertGreater(im.width, 1000)

    def test_audiences_rotate_and_lines_get_their_own_products(self):
        pool = ["a", "b", "c"]
        designs = [{"audience": "a"}, {"audience": "b"}]
        self.assertEqual(ws.pick_audience(pool, designs, "plain"), "c")
        self.assertEqual(ws.pick_audience(pool, designs, "pet"), "c")                   # ties go round in order
        c = FakeClients(shops=[])
        s = self.shop(c)
        rec = s.make_design(style="pet", season="christmas")
        self.assertEqual(rec["only"], ws.LINES["pet"]["products"])
        self.assertIn(rec["audience"], ws.LINES["pet"]["audiences"])
        plain = s.make_design()
        self.assertIn(plain["audience"], ws.AUDIENCES)

    def test_the_gift_extras_never_go_on_etsy_where_each_listing_costs(self):
        extras = {"pajamas", "puzzle", "card", "doormat", "desk_mat", "can_cooler", "emb_hat", "emb_beanie", "wrap",
                  "stocking", "blanket", "baby_bodysuit"}
        self.assertFalse(extras & set(ws.DEFAULTS["etsy_products"]))
        self.assertEqual(ws.DEFAULTS["etsy_per_design"], 3)

    def test_christmas_days_rotate_lines_with_a_sweater_every_day(self):
        for d in range(8):
            day = datetime(2026, 11, 1 + d).date()
            styles = [ws.christmas_style(day, i, 3) for i in range(3)]
            self.assertEqual(styles[1], "sweater")
            self.assertNotEqual(styles[0], styles[2])
        firsts = [ws.christmas_style(datetime(2026, 11, 1 + d).date(), 0, 3) for d in range(6)]
        self.assertEqual(sorted(firsts), ["job"] * 3 + ["pet"] * 3)                   # Etsy's pick: niche lines only
        thirds = {ws.christmas_style(datetime(2026, 11, 1 + d).date(), 2, 3) for d in range(6)}
        self.assertEqual(thirds, {"couple", "family"})

    def test_a_pet_design_brings_more_breeds_that_stay_off_etsy(self):
        c = FakeClients(shops=[])
        s = self.shop(c, line_series=True)
        first = s.make_design(style="pet", season="christmas")
        st = ws.load_state(s.state_path)
        self.assertEqual(st["line_queue"], ["pet", "pet"])
        extra = s.make_design(style="pet", season="christmas", series=True)
        self.assertTrue(extra["series"])
        self.assertNotEqual(extra["audience"], first["audience"])                      # a different breed
        st = ws.load_state(s.state_path)
        self.assertEqual(st["line_queue"], ["pet", "pet"])                             # extras don't bring more
        self.assertFalse(s.on_etsy(extra, st))

    def test_job_line_is_retro_and_aimed_at_one_job(self):
        c = FakeClients(shops=[])
        s = self.shop(c)
        rec = s.make_design(style="job", season="christmas")
        self.assertEqual(rec["look"], "retro")
        self.assertIn(rec["audience"], ws.JOBS)
        self.assertIn(rec["audience"], c.notes)
        self.assertEqual(rec["only"], ws.LINES["job"]["products"])
        with Image.open(rec["file"]) as im:
            self.assertEqual(im.mode, "RGBA")
            self.assertGreater(im.width, 2500)

    def test_lines_without_their_own_look_take_turns_between_retro_and_plain(self):
        self.assertEqual(ws.Shop.look_for("pet", []), "retro")
        self.assertEqual(ws.Shop.look_for("pet", [{"style": "pet", "look": "retro"}]), "plain")
        self.assertEqual(ws.Shop.look_for("pet", [{"style": "pet", "look": "plain"}]), "retro")
        self.assertEqual(ws.Shop.look_for("couple", []), "plain")

    def test_loose_specks_beside_the_drawing_are_cleared(self):
        img = Image.new("RGB", (400, 300), "white")
        d = ImageDraw.Draw(img)
        d.ellipse((50, 50, 250, 250), fill=(200, 30, 30))                         # the drawing
        d.ellipse((120, 120, 130, 130), fill=(255, 255, 255))                     # white inside it stays
        d.rectangle((350, 140, 353, 146), fill=(40, 40, 40))                      # a stray mark off to the side
        out = ws.cut_background(img)
        self.assertLess(out.width, 260)                                           # cropped to the drawing alone

    def test_video_shirts_keep_real_people_out(self):
        self.assertEqual(ws.real_names("Could prime LeBron James beat Satan in a 1v1?", ["LeBron James"])[:3],
                         ["LeBron James", "LeBron", "James"])
        self.assertEqual(ws.real_names("What if you fell into Jupiter?", []), [])
        bad = dict(IDEA, slogan="LEBRON WOULD COOK", real_people=["LeBron James"])
        good = dict(IDEA, slogan="1V1 ME SATAN", title="Funny Devil Basketball 1v1 Shirt", real_people=["LeBron James"])
        c = FakeClients(shops=[])
        c.ideas = [bad, good]
        s = self.shop(c)
        rec = s.make_design(video="Could prime LeBron James beat Satan in a 1v1?")
        self.assertEqual(rec["slogan"], "1V1 ME SATAN")                              # the LeBron one was refused
        self.assertEqual(rec["style"], "video")
        self.assertFalse(rec["etsy"])
        self.assertEqual(rec["only"], ws.VIDEO_PRODUCTS)
        self.assertIn("no real person", c.notes)

    def test_each_posted_video_topic_gets_one_shirt(self):
        viral = self.dir / "viral.json"
        viral.write_text(json.dumps({"videos": [
            {"topic": "falling into Jupiter", "made": 1, "posted": True},
            {"topic": "Falling into Jupiter", "made": 2, "posted": True},          # the same topic again
            {"topic": "a black hole Sun", "made": 3, "posted": False}]}))           # not posted yet
        self.assertEqual(ws.video_topics(viral), ["falling into Jupiter"])
        c = FakeClients(shops=[])
        s = self.shop(c, designs_per_day=0, holiday_per_day=0, video_shirts=True)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: None)
        svc.viral_state = viral
        with patch.object(ws, "settings", return_value=dict(s.cfg(), enabled=True)), \
                patch.object(svc, "storefront"), patch.object(svc, "etsy_photos"), \
                patch.object(svc, "video_slot"), patch.object(svc, "evening_review"):
            svc.run_once(s, now=datetime(2026, 11, 20, 12))
            svc.run_once(s, now=datetime(2026, 11, 20, 12, 5))
        st = ws.load_state(s.state_path)
        self.assertEqual([d.get("video") for d in st["designs"]], ["falling into Jupiter"])
        self.assertEqual(st["video_shirts"], ["falling into Jupiter"])

    def test_retro_design_arches_short_slogans_and_splits_long_ones(self):
        from worker import retro
        self.assertEqual(retro.split("MERRY CORGMAS"), ("MERRY CORGMAS", ""))
        top, bottom = retro.split("SANTA CLAWS IS COMING")
        self.assertTrue(top and bottom)
        self.assertEqual(f"{top} {bottom}", "SANTA CLAWS IS COMING")
        art = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        art.paste((200, 30, 30, 255), (100, 100, 300, 300))
        out = retro.design(art, "SANTA CLAWS IS COMING", seed=1)
        self.assertEqual(out.width, retro.WIDTH)
        self.assertEqual(out.getpixel((0, 0))[3], 0)                                  # transparent around it

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

    def test_etsy_gets_one_holiday_design_a_day_on_its_products_ebay_stops_at_its_free_limit(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}, {"id": 5, "sales_channel": "ebay"},
                               {"id": 7, "sales_channel": "storefront"}])
        s = self.shop(c, etsy_per_day=1, etsy_holiday_only=True, etsy_products=["tee", "mug"], ebay_max=4,
                      etsy_lines=["pet", "job"])
        three = ["tee", "mug", "sticker"]
        s.make_design()                                   # an everyday design: not for Etsy
        s.make_design(only=three, season="christmas", style="sweater")   # a holiday design off the niche lines: not Etsy
        s.make_design(only=three, season="christmas", style="job")    # the day's niche pick
        s.make_design(only=three, season="christmas", style="pet")    # a second one: over Etsy's one a day
        s.list_pending()
        posts = lambda shop: [b for m, p, b in c.calls if m == "POST" and p == f"shops/{shop}/products.json"]
        self.assertEqual(len(posts(9)), 2)                # tee + mug of the first holiday design, no sticker
        self.assertEqual(len(posts(5)), 4)                # eBay stops at its limit
        self.assertEqual(len(posts(7)), 12)               # the Pop-Up Store gets everything
        designs = ws.load_state(self.dir / "s.json")["designs"]
        self.assertEqual([d["etsy"] for d in designs], [False, False, True, False])
        self.assertEqual(len([t for k, t in self.sent if "free limit" in t]), 1)
        s.list_pending()                                  # nothing more, and no second text
        self.assertEqual((len(posts(9)), len(posts(5))), (2, 4))
        self.assertEqual(len([t for k, t in self.sent if "free limit" in t]), 1)

    def test_tiktok_shop_gets_newest_clean_designs_up_to_its_daily_cap(self):
        c = FakeClients(shops=[{"id": 4, "sales_channel": "tiktok"}])
        three = ["tee", "mug", "sticker"]
        s = self.shop(c, tiktok_per_day=4, tiktok_products=three)
        s.make_design(only=three)                         # oldest: past today's cap
        s.make_design(only=three)
        s.make_design(only=three)
        st = ws.load_state(self.dir / "s.json")
        st["designs"][2]["slogan"] = "WHAT THE F*CK, SHIT HAPPENS"   # swearing: never on TikTok
        ws.save_state(st, self.dir / "s.json")
        done = s.list_pending()
        self.assertEqual(len(done), 4)
        self.assertTrue(all("on TikTok Shop" in d for d in done))
        designs = ws.load_state(self.dir / "s.json")["designs"]
        self.assertEqual([len(d.get("channels", {}).get("tiktok", {})) for d in designs], [1, 3, 0])
        self.assertEqual(s.list_pending(), [])            # the cap holds for the rest of the day

    def test_tiktok_shop_only_gets_the_kinds_it_accepts(self):
        c = FakeClients(shops=[{"id": 4, "sales_channel": "tiktok"}])
        s = self.shop(c)                                  # TikTok refuses products printed outside the US
        s.make_design(only=["tee", "mug", "hoodie"])
        s.list_pending()
        rec = ws.load_state(self.dir / "s.json")["designs"][0]
        self.assertEqual(sorted(rec["channels"]["tiktok"]), ["hoodie", "tee"])
        self.assertEqual(s.list_pending(), [])            # the mug doesn't use up tomorrow's budget either

    def test_ebay_rotation_ends_the_oldest_unsold_listing_for_a_winners_spin_off(self):
        c = FakeClients(shops=[{"id": 5, "sales_channel": "ebay"}])
        s = self.shop(c, ebay_max=2, ebay_rotate=True)
        s.make_design(only=["tee", "mug"])                # fills eBay
        s.list_pending()
        st = ws.load_state(self.dir / "s.json")
        st["designs"][0]["id"] = "old"
        ws.save_state(st, self.dir / "s.json")
        s.make_design(only=["tee"])
        st = ws.load_state(self.dir / "s.json")
        st["designs"][1]["spinoff_of"] = "w"
        ws.save_state(st, self.dir / "s.json")
        s.list_pending()
        designs = ws.load_state(self.dir / "s.json")["designs"]
        self.assertEqual(list(designs[1]["channels"]["ebay"]), ["tee"])
        self.assertEqual(designs[0]["ebay_ended"], ["tee"])
        self.assertEqual(len([1 for m, p, b in c.calls if m == "DELETE"]), 1)
        s.list_pending()                                  # the ended one isn't put back
        self.assertEqual(len([1 for m, p, b in c.calls if m == "DELETE"]), 1)
        self.assertNotIn("tee", ws.load_state(self.dir / "s.json")["designs"][0]["channels"]["ebay"])

    def test_without_rotation_a_full_ebay_takes_nothing_down(self):
        c = FakeClients(shops=[{"id": 5, "sales_channel": "ebay"}])
        s = self.shop(c, ebay_max=1)
        s.make_design(only=["tee"])
        s.make_design(only=["tee"])
        st = ws.load_state(self.dir / "s.json")
        st["designs"][1]["spinoff_of"] = "w"
        ws.save_state(st, self.dir / "s.json")
        s.list_pending()
        self.assertFalse([1 for m, p, b in c.calls if m == "DELETE"])

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


class PrintQualityTests(_Base):
    """2026-11-10, "for our products we post, make them even better": what the listings' own photos showed was wrong."""

    def test_a_long_slogan_is_set_whole_and_big_and_the_print_keeps_a_shirts_shape(self):
        from worker import lettering
        slogan = "SCIENTISTS CONFIRM GRANDMA'S HOUSE ISN'T GROUNDED"      # ran off the bottom of its shirt before
        rows = lettering.layout_lines(slogan, 3000)
        self.assertEqual(" ".join(line for line, _ in rows), slogan)
        self.assertLessEqual(len(rows), 3)
        self.assertGreater(min(size for _, size in rows), 250)             # letters over 8% of the width tall
        for line, size in rows:
            self.assertLessEqual(lettering.font("block", size).getlength(line), 3000 + 1)
        design = ws.compose(ws.cut_background(Image.open(io.BytesIO(_png()))), slogan)
        self.assertEqual(design.width, ws.DESIGN_W)
        self.assertGreater(design.width / design.height, 0.8)              # printed at full width, not shrunk to fit

    def test_small_words_never_get_a_line_to_themselves(self):
        from worker import lettering
        for slogan in ("IT HEARD THE COOKIE TIN OPEN", "READING ALCOHOLICALLY TO AVOID FAMILY"):
            rows = lettering.layout_lines(slogan, 1600, max_height=1600)   # the tall block a wide layout allows
            self.assertFalse([line for line, _ in rows if line in ("THE", "TO", "IT")], slogan)

    def test_the_retro_arch_ends_where_the_sentence_pauses(self):
        from worker import retro
        self.assertEqual(retro.split("WHAT AM I, THE GIFT SHREDDING FAIRY?"), ("WHAT AM I,", "THE GIFT SHREDDING FAIRY?"))
        self.assertEqual(retro.split("COOKIES GO IN. CHAOS COMES OUT."), ("COOKIES GO IN.", "CHAOS COMES OUT."))
        self.assertEqual(retro.split("100 AURA FOR MAKING IT TO LUNCH")[0], "100 AURA")       # not left hanging on "FOR"
        self.assertEqual(retro.split("ORNAMENTALLY UNSTABLE"), ("ORNAMENTALLY", "UNSTABLE"))  # the arch stays readable

    def test_sweater_designs_are_knitted_with_the_picture_the_designer_picked(self):
        from worker import sweater
        c = FakeClients(shops=[])
        c.draw = lambda prompt: self.fail("the sweater art is drawn locally")
        c.ideas = [dict(IDEA, slogan="SLEIGHING BY A THREAD", motif="Reindeer")]
        s = self.shop(c)
        rec = s.make_design(season="christmas", style="sweater")
        self.assertEqual((rec["motif"], rec["v"]), ("reindeer", 2))
        self.assertIn('"motif"', c.notes)                                  # the Designer is told to choose one
        with Image.open(rec["file"]) as im:
            self.assertEqual(im.width, sweater.ACROSS * sweater.STITCH)
        c.ideas = [dict(IDEA, slogan="WE'RE ALL SLEIGHING BY A THREAD", motif="snowman")]
        variant = s.make_design(spinoff=rec["id"], variant=True)
        self.assertEqual(variant["motif"], "reindeer")                     # a close variant keeps the winner's picture
        c.ideas = [dict(IDEA, slogan="GET LIT", motif="a dragon")]
        self.assertEqual(s.make_design(season="christmas", style="sweater")["motif"], "tree")   # not one we knit
        c.draw = lambda prompt: _png()
        self.assertNotIn("motif", s.make_design())                         # only the sweater line has one
        for name in sweater.MOTIFS:
            chart, lights = sweater.motif(name, seed=3)
            self.assertGreater(chart.getbbox()[2], 60, name)               # every picture draws something

    def test_knitted_letters_go_bold_when_they_fit_and_never_past_the_edge(self):
        from worker import sweater
        lines, scale, bold = sweater.fit_lines("I'M LOSING MY TINSEL", 180)
        self.assertEqual((lines, scale, bold), (["I'M LOSING", "MY TINSEL"], 3, True))
        for slogan in ("ELF-DESTRUCTING SINCE NOVEMBER", "IMMINENT MENTAL BREAKDOWN AVALANCHE", "GET LIT", "HO"):
            lines, scale, bold = sweater.fit_lines(slogan, 180)
            self.assertEqual(" ".join(lines), slogan)
            self.assertTrue(all(sweater.text_width(line, bold) * scale <= 180 for line in lines), slogan)
        self.assertEqual(len(sweater._glyph("M", bold=True)[0]), 7)        # its own chart: thickened, an M fills in

    def test_a_hoodie_takes_the_wide_layout_and_a_white_shirt_gets_dark_letters(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        rec = s.make_design(season="christmas", style="sweater")
        rec["upload_id"] = s.upload(rec)
        s.create(rec, "sweater_tee", 9, rec["upload_id"])                  # what it was drawn for: a dark shirt
        s.create(rec, "tee", 9, rec["upload_id"])                          # a white one
        made = s.create(rec, "hoodie", 9, rec["upload_id"])
        bodies = [b for m, p, b in c.calls if m == "POST" and p.endswith("/products.json")]
        art = lambda b: b["print_areas"][0]["placeholders"][0]["images"][0]["id"]
        self.assertEqual(art(bodies[0]), rec["upload_id"])
        self.assertEqual(len({art(b) for b in bodies}), 3)                 # three renderings of the one design
        self.assertEqual(set(rec["variants"]), {"tall-light", "wide-light"})
        self.assertEqual(made["art"], "wide-light")
        wide = rec["variants"]["wide-light"]["size"]
        self.assertGreater(wide[0] / wide[1], 1.3)                         # for the short area above a hoodie's pocket

        def ink(path):                                                     # the commonest colour where the words are
            with Image.open(path) as im:
                band = im.crop((0, 420, im.width, 700))
                return max((n, c) for n, c in band.getcolors(1 << 22) if c[3] == 255)[1]
        self.assertGreater(sum(ink(rec["file"])[:3]), 600)                 # cream on the dark shirt
        self.assertLess(sum(ink(self.dir / "art" / f"{rec['id']}-tall-light.png")[:3]), 330)   # dark green on the white
        s.create(rec, "tee", 9, rec["upload_id"])                          # drawn and uploaded once
        self.assertEqual(len([1 for m, p, b in c.calls if p == "uploads/images.json"]), 3)

    def test_an_ornament_is_sold_round_and_oval_with_the_design_inside_each(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        rec = s.make_design()
        s.create(rec, "ornament", 9, "main")
        body = next(b for m, p, b in c.calls if m == "POST" and p.endswith("/products.json"))
        self.assertEqual([v["id"] for v in body["variants"]], [15990, 15992])          # no heart, no star: they cut the words
        areas = {tuple(a["variant_ids"]): a["placeholders"][0]["images"][0] for a in body["print_areas"]}
        self.assertEqual(set(areas), {(15990,), (15992,)})
        w, h = rec["size"]
        for ids, (pw, ph) in (((15990,), (886, 886)), ((15992,), (720, 1004))):
            box_w = areas[ids]["scale"] * pw
            self.assertLess((box_w / pw) ** 2 + (box_w * h / w / ph) ** 2, 1.0)        # its corners are inside the curve
            self.assertGreater(areas[ids]["y"], 0.5)                                   # low, clear of the cord's hole
        self.assertLess(areas[(15990,)]["scale"], areas[(15992,)]["scale"])            # the round face is the smaller

    def test_a_bandana_gets_the_words_on_top_hung_below_the_collar(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        rec = s.make_design()
        s.create(rec, "pet_bandana", 9, "main")
        body = next(b for m, p, b in c.calls if m == "POST" and p.endswith("/products.json"))
        img = body["print_areas"][0]["placeholders"][0]["images"][0]
        self.assertNotEqual(img["id"], "main")
        w, h = rec["variants"]["over-light"]["size"]
        half = img["scale"] * 3150 * h / w / 1691 / 2                      # half the design's height, in print areas
        self.assertGreater(img["y"] - half, 0.31)                          # under the part that rolls at the collar
        self.assertLess(img["y"] + half, 0.9)                              # and done before the point
        from worker import lettering
        art = ws.cut_background(Image.open(io.BytesIO(_png())))
        inked = lambda im: lettering.INK in {c for n, c in im.crop((0, 0, im.width, int(im.height * 0.4))).getcolors(1 << 22)}
        self.assertTrue(inked(lettering.compose(art, "MERRY CORGMAS TO ME", layout="over")))    # the words lead on a bandana,
        self.assertFalse(inked(lettering.compose(art, "MERRY CORGMAS TO ME", layout="tall")))   # the picture on a shirt

    def test_a_flawed_drawing_is_redrawn_and_a_hopeless_one_is_put_off(self):
        c = FakeClients(shops=[])
        drawn = []
        verdicts = [{"ok": False, "problems": [{"kind": "text", "severity": "major", "what": "the word FARMER"}]},
                    {"ok": True, "problems": [{"kind": "glitch", "severity": "minor", "what": "a stray dot"}]}]
        c.draw = lambda prompt: drawn.append(prompt) or _png()
        c.look = lambda image, prompt: verdicts.pop(0)
        s = self.shop(c)
        rec = s.make_design()
        self.assertEqual((len(drawn), rec["art_checked"]), (2, 2))         # the second drawing passed
        self.assertTrue(Path(rec["art"]).is_file())                        # kept, for the other layouts
        c.look = lambda image, prompt: {"ok": False, "problems": [{"kind": "glitch", "severity": "major", "what": "red eyes"}]}
        with self.assertRaises(RuntimeError):
            s.make_design()
        self.assertEqual(len(ws.load_state(s.state_path)["designs"]), 1)   # nothing listed with a mistake in it
        self.assertEqual(len(drawn), 2 + s.ART_TRIES)
        c.look = lambda image, prompt: None                                # nobody to look: the drawing is used
        self.assertNotIn("art_checked", s.make_design())

    def test_listings_say_what_the_product_is_made_of_and_tags_keep_whole_words(self):
        html = ('<p>A classic tee.</p><div>.:Made with 100% Airlume combed and ring-spun cotton, a lightweight fabric.</div>'
                '<br /><div>.:Available in two sizes: 11oz and 15oz</div><br /><div>.:Blank product sourced from China</div>'
                '<div>.:The tear-away label minimizes skin irritations.</div>')
        facts = ws.product_facts(html)
        self.assertEqual(facts, ["Made with 100% Airlume combed and ring-spun cotton, a lightweight fabric.",
                                 "The tear-away label minimizes skin irritations."])
        text = ws.listing_text({"description": "Pitch.", "slogan": "I'M LOSING MY TINSEL"}, facts)
        self.assertIn('It reads: "I\'m Losing My Tinsel".', text)
        self.assertIn("- Made with 100% Airlume", text)
        self.assertTrue(text.endswith(ws.AI_NOTE))
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c, products=["tee"])
        s.make_design()
        s.list_pending()
        tee = next(b for m, p, b in c.calls if m == "POST" and p.endswith("/products.json"))
        self.assertIn("Details:\n- Material: 100% cotton.", tee["description"])      # from the catalog, sellers' notes dropped
        self.assertNotIn("sourced", tee["description"])
        tags = ws.clean_idea({**IDEA, "tags": ["funny christmas shirt gift", "supercalifragilisticexpialidocious"]})["tags"]
        self.assertEqual(tags, ["funny christmas"])                        # was "funny christmas shir"

    def test_a_sweater_design_already_on_a_white_shirt_is_redrawn_for_it_once(self):
        c = FakeClients(shops=[{"id": 7, "sales_channel": "storefront"}])
        s = self.shop(c)
        s.make_design(season="christmas", style="sweater")
        st = ws.load_state(s.state_path)
        st["designs"][0].update(upload_id="main", channels={"popup": {
            "tee": {"id": "old-tee", "shop": 7, "price": 2299, "published": True, "photos": ["stale"]},
            "sweater_tee": {"id": "old-dark", "shop": 7, "price": 2299, "published": True},
            "hat": {"id": "old-hat", "shop": 7, "price": 1999, "published": True}}})
        ws.save_state(st, s.state_path)
        real = c.printify
        c.printify = lambda m, p, b=None: {"variants": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],     # 4: dropped by the printer
                                           "print_areas": [{"variant_ids": [1, 2, 3]}]} \
            if (m, p) == ("GET", "shops/7/products/old-tee.json") else real(m, p, b)
        svc = ws.ShopService(log=lambda m: None)
        self.assertEqual(svc.repair_art(s), 1)
        puts = [(p, b) for m, p, b in c.calls if m == "PUT"]
        self.assertEqual([p for p, b in puts], ["shops/7/products/old-tee.json"])       # not the dark shirt, not the hat
        self.assertNotEqual(puts[0][1]["print_areas"][0]["placeholders"][0]["images"][0]["id"], "main")
        self.assertEqual(puts[0][1]["print_areas"][0]["variant_ids"], [1, 2, 3])        # every variant its areas cover: Printify insists
        self.assertEqual(len([1 for m, p, b in c.calls if p == "shops/7/products/old-tee/publish.json"]), 1)
        prod = ws.load_state(s.state_path)["designs"][0]["channels"]["popup"]["tee"]
        self.assertEqual(prod["art"], "tall-light")
        self.assertNotIn("photos", prod)                                   # the site fetches the new photos
        self.assertEqual(svc.repair_art(s), 0)                             # once

    def test_a_redraw_that_fails_is_tried_again_hours_later_not_every_pass(self):
        c = FakeClients(shops=[{"id": 7, "sales_channel": "storefront"}])
        s = self.shop(c)
        s.make_design(season="christmas", style="sweater")
        st = ws.load_state(s.state_path)
        st["designs"][0].update(upload_id="main", channels={"popup": {
            "mug": {"id": "old-mug", "shop": 7, "price": 1299, "published": True, "art": "unfixed"}}})   # an earlier failure
        ws.save_state(st, s.state_path)
        real = c.printify
        c.printify = lambda m, p, b=None: (_ for _ in ()).throw(RuntimeError("Printify said no")) if m == "PUT" else real(m, p, b)
        svc = ws.ShopService(log=lambda m: None)
        self.assertEqual(svc.repair_art(s), 1)                             # tried, and it failed
        self.assertEqual(svc.repair_art(s), 0)                             # not again on the next pass
        c.printify = real
        s.clock = lambda: 1_792_900_000 + svc.REPAIR_RETRY + 1
        self.assertEqual(svc.repair_art(s), 1)
        self.assertEqual(ws.load_state(s.state_path)["designs"][0]["channels"]["popup"]["mug"]["art"], "tall-light")


class ScheduleTests(_Base):
    def test_one_design_a_day_after_the_set_time_and_make_now(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        s = self.shop(c)
        svc = ws.ShopService(log=lambda m: None, notify=lambda k, t: self.sent.append((k, t)))
        svc.viral_state = self.dir / "no-viral.json"       # not the real videos' state
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
        svc.viral_state = self.dir / "no-viral.json"       # not the real videos' state
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

    def test_new_designs_get_a_page_and_a_feed_item_per_product(self):
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
        rec = designs[0]
        out = self.dir / "site"
        sf.build(designs, printify, out, {**sf.DEFAULTS, "product_pins_since": rec["made"]},
                 download=lambda url, dest: dest.write_bytes(b"jpg") or True)
        feed = (out / "feed.xml").read_text()
        kinds = list(rec["products"])
        self.assertEqual(feed.count("<item>"), len(kinds))
        page = (out / "p" / f"{rec['id']}-{kinds[0]}.html").read_text()
        self.assertIn("Buy on Etsy", page)
        self.assertIn(f"https://nathan123y.github.io/p/{rec['id']}-{kinds[0]}.html", feed)
        self.assertNotIn(f"https://nathan123y.github.io/p/{rec['id']}.html<", feed)     # no design-level item too

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

    def test_a_kind_without_a_shot_list_saves_every_other_angle(self):
        s = self.shop(FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}]))
        rec = s.make_design()
        imgs = [{"src": f"https://x/m.jpg?camera_label={c}", "variant_ids": [1]}
                for c in ("front", "front-2", "close-up", "context-1", "front")]
        with patch("requests.get", lambda *a, **k: type("R", (), {"status_code": 200, "content": b"x" * 6000})()):
            saved = s.save_photos(rec, "poster", imgs)
            self.assertEqual([Path(p).name.split("poster-")[-1] for p in saved if "-poster-" in p],
                             ["front-2.jpg", "close-up.jpg", "context-1.jpg"])
            self.assertFalse([p for p in s.save_photos(rec, "mug", imgs) if "-mug-" in p])   # side views left out

    def test_the_ugly_sweater_line_prints_every_panel_and_skips_the_ai_drawing(self):
        c = FakeClients(shops=[{"id": 9, "sales_channel": "etsy"}])
        c.draw = lambda prompt: self.fail("the sweater art is drawn locally")
        s = self.shop(c)
        rec = s.make_design(season="christmas", style="sweater")
        self.assertEqual(rec["only"], ws.SWEATER_PRODUCTS)
        prod = s.create(rec, "ugly_sweater", 9, "unused")
        body = next(b for m, p, b in c.calls if m == "POST" and p.endswith("/products.json"))
        self.assertEqual([v["id"] for v in body["variants"]], [10230])                       # the chosen sizes only
        self.assertEqual({h["position"] for h in body["print_areas"][0]["placeholders"]},
                         {"front", "back", "right_sleeve", "left_sleeve", "collar", "waistband"})
        self.assertTrue(body["title"].endswith("Ugly Christmas Sweater"))
        self.assertEqual(body["title"].lower().count("ugly christmas sweater"), 1)
        self.assertGreater(prod["price"], 3100)

    def test_sweater_titles_lead_with_the_search_words(self):
        self.assertEqual(ws.sweater_title("funny christmas sweater shirt sarcastic holiday sweatshirt xmas gift"),
                         "Ugly Christmas Sweater Funny Christmas Shirt Sarcastic Holiday Xmas Gift")
        self.assertEqual(ws.sweater_title("Funny Ugly Christmas Sweat Shirt Gag Gift"), "Ugly Christmas Sweater Funny Gag Gift")

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


class SeasonTests(unittest.TestCase):
    def test_holiday_lines_run_in_their_windows_and_take_turns(self):
        from datetime import date
        self.assertEqual(ws.season_for(date(2026, 10, 27)), "christmas")
        self.assertEqual({ws.season_for(date(2026, 11, 1)), ws.season_for(date(2026, 11, 2))}, {"christmas", "thanksgiving"})
        self.assertEqual(ws.season_for(date(2026, 11, 25)), "christmas")
        self.assertIsNone(ws.season_for(date(2026, 12, 20)))


if __name__ == "__main__":
    unittest.main()
