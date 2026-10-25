import unittest
from datetime import datetime, timedelta, timezone

from worker import ig_scout

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
ts = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeGraph:
    def __init__(self):
        self.urls = []

    def __call__(self, url, params):
        self.urls.append((url, dict(params)))
        path = url.replace(ig_scout.GRAPH, "")

        class R:
            status_code, content = 200, b"x"

            def __init__(s, d):
                s.d = d

            def json(s):
                return s.d
        if path == "me/accounts":
            return R({"data": [{"instagram_business_account": {"id": "17841"}}]})
        if path == "ig_hashtag_search":
            return R({"data": [{"id": "h1"}]})
        if path == "h1/top_media":
            return R({"data": [{"id": "t1", "like_count": 900, "comments_count": 10, "timestamp": ts(3), "media_type": "IMAGE",
                                "media_url": "http://img/t1", "permalink": "p/t1", "caption": "when the code works first try"}]})
        fields = params.get("fields", "")
        if "business_discovery.username(9gag)" in fields:
            return R({"business_discovery": {"followers_count": 60_000_000, "media": {"data": [
                {"id": "a", "like_count": 120_000, "comments_count": 900, "timestamp": ts(4), "media_type": "IMAGE",
                 "media_url": "http://img/a", "permalink": "p/a", "caption": "tag a friend"},
                {"id": "old", "like_count": 900_000, "comments_count": 9, "timestamp": ts(100), "media_type": "IMAGE",
                 "media_url": "http://img/old", "permalink": "p/old"}]}}})
        return type("E", (), {"status_code": 400, "content": b"x", "json": lambda s: {"error": {"message": "not a business account"}}})()


class IgScoutTests(unittest.TestCase):
    def test_reads_pages_and_hashtags_ranks_fresh_viral_posts_and_never_writes(self):
        g = FakeGraph()
        logs = []
        lines = ig_scout.fetch(log=logs.append, reader=ig_scout.Reader("tok", g), now=NOW,
                               read=lambda url: "SKIP" and None if url.endswith("t1") else "\"me pretending to work\" office cat")
        self.assertEqual(len(lines), 2)
        self.assertIn("120.0k likes", lines[0])
        self.assertIn("@9gag", lines[0])
        self.assertIn("me pretending to work", lines[0])
        self.assertIn("caption: when the code works first try", lines[1])     # picture unreadable: the caption instead
        self.assertFalse(any("old" in l for l in lines))                      # older than 48 hours
        self.assertTrue(any("@pubity skipped" in l for l in logs))           # a page that fails is skipped, not fatal
        self.assertTrue(all(u.startswith(ig_scout.GRAPH) for u, _ in g.urls))
        self.assertFalse(any("media_publish" in u or u.endswith("/media") for u, _ in g.urls))

    def test_not_connected_is_nothing(self):
        orig = ig_scout._cfg
        ig_scout._cfg = lambda: {}
        self.addCleanup(lambda: setattr(ig_scout, "_cfg", orig))
        self.assertEqual(ig_scout.fetch(), [])


if __name__ == "__main__":
    unittest.main()
