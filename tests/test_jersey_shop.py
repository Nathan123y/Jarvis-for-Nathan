"""The jersey line: inbox art -> a draft and a text -> approved from the phone -> listed (TikTok, eBay, Pop-Up), Etsy slowly."""
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from worker import jersey_shop as js

STORES = {"popup": {"id": 1}, "tiktok": {"id": 2}, "ebay": {"id": 3}, "etsy": {"id": 4}}
PLACE = {"basketball": [("front", 3529, 4636), ("back", 3519, 4625)],
         "soccer": [("front", 3640, 4250), ("back", 3644, 4248), ("right_sleeve", 3119, 1544),
                    ("left_sleeve", 3119, 1544), ("Collar", 3662, 319)]}


class FakeClients:
    def __init__(self):
        self.calls, self.n = [], 0

    def printify(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("variants.json"):
            kind = "soccer" if "1332" in path else "basketball"
            return {"variants": [{"id": i, "placeholders": [{"position": p, "width": w, "height": h}
                                                             for p, w, h in PLACE[kind]]} for i in (10, 11)]}
        if path.startswith("catalog/blueprints/"):
            return {"description": "<div>.:100% polyester</div><div>.:Regular fit</div>"}
        if path == "uploads/images.json":
            self.n += 1
            return {"id": f"up{self.n}"}
        if method == "POST" and path.endswith("products.json"):
            self.n += 1
            return {"id": f"p{self.n}", "variants": [{"id": 10, "cost": 2258}, {"id": 11, "cost": 2503}],
                    "images": []}
        return {}


class FakeShop:
    def __init__(self, tmp, tiktok_live=50, ebay_count=0):
        self.c = FakeClients()
        self.state_path = tmp / "etsy_shop.json"
        designs = [{"id": "old1", "slogan": "OLD", "channels": {"tiktok": {"tee": {"id": "t1", "published": True}},
                                                                 "ebay": {"tee": {"id": f"e{i}"} for i in range(ebay_count)}}},
                   {"id": "won", "slogan": "SOLD", "channels": {"tiktok": {"tee": {"id": "t2", "published": True}}}}]
        self.state_path.write_text(json.dumps({"designs": designs, "sales_log": [{"designs": ["won"]}]}))
        self.live, self.published, self.ended_ebay = tiktok_live, [], 0

    def stores(self):
        return STORES

    def publish(self, prod):
        prod["published"] = True
        self.published.append(prod["shop"])

    def tiktok_live(self, shop_id):
        return self.live

    def cfg(self):
        return {"ebay_max": 1}

    def end_oldest_ebay(self, st, store, sold):
        self.ended_ebay += 1
        return True


NOON = 1_795_118_400.0                                  # 2026-11-19 noon-ish: texts go out in the day


def make(tmp, shop, clock=lambda: NOON, auto=False):
    texts, files = [], []
    j = js.Jerseys(shop, text_file=lambda p, n: files.append((p, n)), text=texts.append, log=lambda m: None,
                   clock=clock, state_path=tmp / "jersey.json", inbox=tmp / "inbox", designs_dir=tmp / "designs",
                   auto=lambda: auto)
    return j, texts, files


def drop(tmp, name, sleeve=False):
    d = tmp / "inbox" / name
    d.mkdir(parents=True)
    for stem in ("front", "back") + (("sleeve",) if sleeve else ()):
        Image.new("RGB", (1024, 1536), (20, 30, 70)).save(d / f"{stem}.png")
    if not sleeve:
        (d / "info.txt").write_text("basketball\n")            # new art is soccer unless info.txt says basketball
    return d




class JerseyTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()


    def test_new_art_is_soccer_unless_it_says_basketball(self):
        d = self.tmp / "x"
        d.mkdir()
        self.assertEqual(js.read_info(d), {})
        (d / "info.txt").write_text("name: FROST\nbasketball\n")
        self.assertEqual(js.read_info(d).get("kind"), "basketball")

    def test_fit_fills_the_print_area(self):
        tmp_path = self.tmp
        out = js.fit(Image.new("RGB", (1024, 1536)), (3529, 4636))
        assert out.size == (3529, 4636)

    def test_inbox_art_becomes_a_draft_and_a_text(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "starfire")
        j, texts, files = make(tmp_path, shop)
        assert j.run() is False                                 # nothing live yet: it waits for the user's yes
        rec = js.load(tmp_path / "jersey.json")["designs"][0]
        assert rec["status"] == "awaiting" and rec["kind"] == "basketball"
        assert set(rec["uploads"]) == {"front", "back"}
        assert rec["channels"]["popup"]["jersey"]["published"] is False
        assert files and "approve jersey starfire" in files[0][1]
        assert not (tmp_path / "inbox" / "starfire").exists()
        assert shop.published == []

    def test_soccer_gets_sleeves_and_collar(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "lone-star-fc", sleeve=True)
        j, _, _ = make(tmp_path, shop)
        j.run()
        rec = js.load(tmp_path / "jersey.json")["designs"][0]
        assert rec["kind"] == "soccer"
        assert set(rec["uploads"]) == {"front", "back", "right_sleeve", "left_sleeve", "Collar"}

    def test_approved_goes_on_tiktok_ebay_and_popup_then_etsy(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "starfire")
        j, texts, _ = make(tmp_path, shop)
        j.run()
        assert "will go live" in js.mark("starfire", "approved", tmp_path / "jersey.json")
        assert j.run() is True
        rec = js.load(tmp_path / "jersey.json")["designs"][0]
        assert rec["status"] == "listed"
        assert {ch for ch, p in rec["channels"].items() if p["jersey"]["published"]} == {"tiktok", "ebay", "popup"}
        assert rec["products"]["jersey"]["published"]           # Etsy too: no jersey there in the last 3 days
        assert any("Jersey live" in t for t in texts)
        prices = [b for m, p, b in shop.c.calls if m == "PUT" and b and "variants" in b]
        assert prices and all(v["price"] % 100 == 99 for v in prices[0]["variants"])

    def test_etsy_waits_its_turn(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        now = [NOON]
        j, _, _ = make(tmp_path, shop, clock=lambda: now[0])
        drop(tmp_path, "starfire")
        drop(tmp_path, "violet-crown")
        j.run()
        js.mark("all", "approved", tmp_path / "jersey.json")
        j.run()
        on_etsy = lambda: [r["slug"] for r in js.load(tmp_path / "jersey.json")["designs"] if (r.get("products") or {}).get("jersey")]
        assert on_etsy() == ["starfire"]
        now[0] += 86400
        j.run()
        assert on_etsy() == ["starfire"]
        now[0] += 3 * 86400
        j.run()
        assert on_etsy() == ["starfire", "violet-crown"]

    def test_tiktok_full_drops_the_oldest_never_sold_shirt(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path, tiktok_live=100)
        j, _, _ = make(tmp_path, shop)
        assert j.make_room_tiktok({"id": 2}) is True
        deleted = [p for m, p, _ in shop.c.calls if m == "DELETE"]
        assert deleted == ["shops/2/products/t1.json"]          # not "won", which sold
        st = json.loads(shop.state_path.read_text())
        assert "tee" not in st["designs"][0]["channels"]["tiktok"]

    def test_ebay_full_ends_an_old_listing(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path, ebay_count=3)
        j, _, _ = make(tmp_path, shop)
        assert j.make_room_ebay({"id": 3}) is True
        assert shop.ended_ebay == 1

    def test_reject_deletes_the_draft(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "starfire")
        j, _, _ = make(tmp_path, shop)
        j.run()
        js.mark("starfire", "rejecting", tmp_path / "jersey.json")
        j.run()
        rec = js.load(tmp_path / "jersey.json")["designs"][0]
        assert rec["status"] == "rejected"
        assert any(m == "DELETE" for m, _, _ in shop.c.calls)

    def test_auto_approve_lists_without_asking(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "starfire")
        j, _, files = make(tmp_path, shop, auto=True)
        j.run()                                             # drawn and approved on this pass
        j.run()                                             # listed on the next
        rec = js.load(tmp_path / "jersey.json")["designs"][0]
        self.assertEqual(rec["status"], "listed")
        self.assertEqual(files, [])                         # no approval text

    def test_no_texts_at_night(self):
        tmp_path = self.tmp
        texts = []
        j = js.Jerseys(FakeShop(tmp_path), text=texts.append, text_file=lambda p, n: None, log=lambda m: None,
                       clock=lambda: NOON + 14 * 3600, state_path=tmp_path / "j.json")
        j._say("hi")
        self.assertEqual(texts, [])

    def test_pin_recs_only_listed(self):
        tmp_path = self.tmp
        shop = FakeShop(tmp_path)
        drop(tmp_path, "starfire")
        j, _, _ = make(tmp_path, shop)
        j.run()
        assert js.pin_recs(tmp_path / "jersey.json") == []
        js.mark("starfire", "approved", tmp_path / "jersey.json")
        j.run()
        recs = js.pin_recs(tmp_path / "jersey.json")
        assert recs[0]["channels"]["tiktok"]["jersey"]["published"]
        assert "STARFIRE" in recs[0]["slogan"]



class TitleTests(unittest.TestCase):
    def test_short_clickable_titles(self):
        from worker.jersey_shop import titles
        xmas = titles({"concept": "NORTH POLE", "kind": "basketball", "title": "North Pole Christmas Basketball Jersey Ugly"})
        self.assertEqual(set(xmas.values()), {"North Pole Christmas Basketball Jersey"})
        plain = titles({"concept": "starfire", "kind": "soccer", "title": "Starfire Vintage Y2K Streetwear Navy"})
        self.assertEqual(plain["tiktok"], "Starfire Vintage Soccer Jersey")
        self.assertLessEqual(len(plain["ebay"].split()), 6)


if __name__ == "__main__":
    unittest.main()
