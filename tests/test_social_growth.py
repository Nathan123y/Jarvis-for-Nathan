"""The 11-23 growth plan: TikTok pause + warm-up, delayed Reels, Instagram captions, the health pause, winner
follow-ups on YouTube, and the weekly scorecard."""
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from worker import guides, social_scorecard as sc, viral as wv


class TikTokSwitchTests(unittest.TestCase):
    def test_warm_up_day_and_switch(self):
        cfg = {"post": True, "tiktok": True, "tiktok_from": "2026-11-27"}
        self.assertFalse(wv.tiktok_on(cfg, "2026-11-26"))
        self.assertTrue(wv.tiktok_on(cfg, "2026-11-27"))
        self.assertFalse(wv.tiktok_on(dict(cfg, tiktok=False), "2026-12-01"))
        self.assertFalse(wv.tiktok_on(dict(cfg, post=False), "2026-12-01"))


class CaptionTests(unittest.TestCase):
    def test_few_tags_and_a_share_line(self):
        out = wv.ig_caption("Foul or flop? #fyp #a #b #c #d #e #f", 5, share=True)
        self.assertIn(wv.IG_SHARE, out)
        self.assertEqual(sum(w.startswith("#") for w in out.split()), 5)
        self.assertNotIn("#fyp", out)

    def test_clips_and_guides_keep_the_old_caption(self):
        self.assertEqual(wv.ig_caption("hi #fyp #gta6"), "hi #reels #gta6")


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "viral.json"
        self.video = Path(self.tmp.name) / "v.mp4"
        self.video.write_bytes(b"x")
        self.rec = {"title": "t", "caption": "c", "path": str(self.video), "job": "", "qa_ok": True}
        wv.save_state({"videos": [dict(self.rec)], "slots": {}}, self.state)
        self.svc = wv.ViralService.__new__(wv.ViralService)
        self.svc.log = lambda m: None
        self.p = [patch.object(wv, "STATE", self.state), patch.object(wv.load_state, "__defaults__", (self.state,)),
                  patch.object(wv.save_state, "__defaults__", (self.state,))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def test_reel_waits_after_the_tiktok_then_goes(self):
        cfg = {"post": True, "instagram": True, "tiktok": True, "ig_delay_min": 90}
        with patch.object(self.svc, "_deliver_tiktok", return_value="posted"), \
                patch.object(self.svc, "post_instagram", return_value="posted") as ig, \
                patch.object(self.svc, "_text_result") as text, patch.object(wv, "remove_files") as rm:
            self.svc.deliver(dict(self.rec), cfg)
            ig.assert_not_called(), text.assert_not_called(), rm.assert_not_called()
            self.assertEqual(self.svc.post_pending_instagram(cfg, now=time.time() + 60), 0)
            self.assertEqual(self.svc.post_pending_instagram(cfg, now=time.time() + 91 * 60), 1)
            ig.assert_called_once(), rm.assert_called_once()
            self.assertEqual(text.call_args[0][2:], ("posted", "posted"))
        self.assertEqual(wv.load_state(self.state).get("ig_pending"), [])

    def test_paused_tiktok_still_posts_the_reel_right_away(self):
        cfg = {"post": True, "instagram": True, "tiktok": False, "ig_delay_min": 90}
        with patch.object(self.svc, "_deliver_tiktok") as tt, \
                patch.object(self.svc, "post_instagram", return_value="posted") as ig, \
                patch("core.phone_alerts.send_text") as text, patch.object(wv, "remove_files") as rm:
            self.assertEqual(self.svc.deliver(dict(self.rec), cfg), "paused")
        tt.assert_not_called(), ig.assert_called_once(), rm.assert_called_once()
        self.assertIn("TikTok: paused", text.call_args[0][0])


class HealthTests(unittest.TestCase):
    def run_health(self, views, cfg=None):
        now = time.time()
        seen = {str(i): {"first": now - 3 * 86400 + i, "views": v} for i, v in enumerate(views)}
        svc = wv.ViralService.__new__(wv.ViralService)
        svc.log = lambda m: None
        with patch.object(wv, "settings", return_value=cfg or {"post": True, "tiktok": True, "health_pause": True}), \
                patch.object(wv, "load_state", return_value={}), \
                patch("memory.config_manager.save_plugin_config") as save:
            msg = svc.check_health(seen, now)
        return msg, save

    def test_three_flops_pause_tiktok(self):
        msg, save = self.run_health([400, 2, 0, 1])
        self.assertIn("TikTok paused", msg)
        save.assert_called_once_with("viral_videos", {"tiktok": False})

    def test_one_decent_post_keeps_it_on(self):
        msg, save = self.run_health([0, 25, 1])
        self.assertEqual(msg, ""), save.assert_not_called()


class FollowupTests(unittest.TestCase):
    def test_a_recent_winner_gets_two_shorts_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(guides, "STATE", Path(tmp) / "g.json"):
            wv.save_state({"videos": [], "short_queue": ["mine"]}, guides.STATE)
            now = time.time()
            stats = {"a": {"title": "Hood friends", "kind": "clip", "views": 4206, "made": now - 86400},
                     "b": {"title": "Old hit", "kind": "short", "views": 1346, "made": now - 10 * 86400},
                     "c": {"title": "Flop", "kind": "short", "views": 4, "made": now - 86400}}
            cfg = {"followups": 2, "followup_views": 500}
            self.assertEqual(len(guides.queue_followups(cfg, lambda m: None, stats)), 2)
            self.assertEqual(guides.queue_followups(cfg, lambda m: None, stats), [])
            q = wv.load_state(guides.STATE)["short_queue"]
            self.assertEqual(len(q), 3)
            self.assertIn("Hood friends", q[0]), self.assertEqual(q[-1], "mine")

    def test_instagram_switch_is_honoured(self):
        with patch("memory.config_manager.get_plugin_config", return_value={"instagram": False}):
            self.assertFalse(guides.settings()["instagram"])


class ScorecardTests(unittest.TestCase):
    def test_monday_once_a_week(self):
        mon = datetime(2026, 11, 30, 9, 5)
        self.assertTrue(sc.due(mon, ""))
        self.assertFalse(sc.due(mon, mon.strftime("%G-W%V")))
        self.assertFalse(sc.due(datetime(2026, 11, 30, 8, 0), ""))
        self.assertFalse(sc.due(datetime(2026, 12, 1, 10, 0), ""))

    def test_text(self):
        now = time.time()
        yt = {"a": {"title": "Hood", "kind": "clip", "views": 4206, "made": now - 86400},
              "b": {"title": "Fish", "kind": "short", "views": 5, "made": now - 86400}}
        viral = {"videos": [{"made": now - 3600, "ig_posted": True}], "tiktok": {}}
        text = sc.build(now, yt, viral, {"tiktok": False})
        self.assertIn("YouTube: 4,211 views on 2 uploads", text)
        self.assertIn("Instagram: 1 of 1", text)
        self.assertIn("TikTok: paused", text)


if __name__ == "__main__":
    unittest.main()
