"""Daily-life plugins: packages, group-chat floods and summaries, money,
routines, YouTube Music, do-not-disturb and the startup reminders."""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import dnd, imessage
from core import startup_brief as sb
from plugins import messages_inbox as inbox
from plugins import mission_control, money_tracker, packages, routines, youtube_music


class FakeConfig:
    """load_api_keys/_save_flag backed by a dict, patched into every module that uses them."""

    def __init__(self, test, modules):
        self.data = {}
        for m in modules:
            if hasattr(m, "load_api_keys"):
                p = patch.object(m, "load_api_keys", side_effect=lambda: dict(self.data))
                p.start()
                test.addCleanup(p.stop)
            if hasattr(m, "_save_flag"):
                p = patch.object(m, "_save_flag", side_effect=self.data.__setitem__)
                p.start()
                test.addCleanup(p.stop)


# ── packages ─────────────────────────────────────────────────────────────────
class PackageClassifier(unittest.TestCase):
    def test_real_world_subjects(self):
        cases = {
            "Your package has been delivered": "delivered",
            "Delivered: Your Amazon.com order #112-555": "delivered",
            "Out for delivery: AirPods case": "out_for_delivery",
            "Your order is arriving today": "out_for_delivery",
            "Your order has shipped!": "shipped",
            "Your package is on its way": "shipped",
            "Delivery attempt failed — action needed": "problem",
            "Your shipment is delayed": "problem",
            "Success, we got your order.": "ordered",
            # promotions and bounces that mention delivery are not packages
            "What can't you get delivered? 🤔": None,
            "Delivery Status Notification (Failure)": None,
            "Enjoy Free Shipping on all orders $50+": None,
            "Your tracked flight to Phoenix is now $521 (was $469)": None,
        }
        for subject, want in cases.items():
            with self.subTest(subject=subject):
                self.assertEqual(packages.classify(subject), want)

    def test_will_be_delivered_is_not_delivered(self):
        self.assertNotEqual(packages.classify("Your order will be delivered Friday"), "delivered")


class PackageScan(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(packages, "STATE_FILE", Path(tmp.name) / "packages_state.json")
        p.start()
        self.addCleanup(p.stop)
        now = int(time.time() * 1000)
        self.mail = {
            "a": {"internalDate": str(now - 60_000), "labelIds": ["CATEGORY_UPDATES"], "snippet": "",
                  "payload": {"headers": [{"name": "From", "value": "Amazon <ship@amazon.com>"},
                                          {"name": "Subject", "value": "Delivered: your order"}]}},
            "b": {"internalDate": str(now - 50_000), "labelIds": ["CATEGORY_PROMOTIONS"], "snippet": "",
                  "payload": {"headers": [{"name": "From", "value": "DoorDash"},
                                          {"name": "Subject", "value": "Your order delivered free"}]}},
            "c": {"internalDate": str(now - 40_000), "labelIds": ["CATEGORY_UPDATES"], "snippet": "",
                  "payload": {"headers": [{"name": "From", "value": "Nike <n@nike.com>"},
                                          {"name": "Subject", "value": "Your order has shipped"}]}},
        }
        service = MagicMock()
        api = service.users.return_value.messages.return_value
        api.list.return_value.execute.return_value = {"messages": [{"id": k} for k in self.mail]}
        api.get.side_effect = lambda userId, id, **kw: MagicMock(execute=MagicMock(return_value=self.mail[id]))
        from plugins import gmail
        for p in (patch.object(gmail, "_connected_accounts", return_value=["personal"]),
                  patch.object(gmail, "_service", return_value=service),
                  patch("memory.config_manager.get_plugin_enabled", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_announces_once_and_skips_promotions(self):
        found = packages.scan()
        self.assertEqual([(p["store"], p["status"]) for p in found],
                         [("Amazon", "delivered"), ("Nike", "shipped")])
        self.assertEqual(packages.scan(), [], "remembered across scans (and restarts)")
        text = packages.announcement([p for p in found if p["status"] in packages.ANNOUNCE])
        self.assertIn("Amazon", text)
        self.assertIn("was delivered", text)
        self.assertNotIn("Nike", text, "shipped is tracked quietly")
        status = packages.run({"action": "status"})
        self.assertIn("Amazon: delivered", status)
        self.assertIn("Nike: shipped", status)

    def test_waits_while_do_not_disturb(self):
        with patch.object(dnd, "is_on", return_value=True):
            self.assertFalse(packages._free())


# ── group chats ──────────────────────────────────────────────────────────────
def gtext(name, cid="g1", label="The Boys", text="lol"):
    return {"kind": "text", "name": name, "chat": label, "chat_id": cid, "group": True,
            "label": label, "at": time.time(), "text": text}


class GroupFloods(unittest.TestCase):
    def setUp(self):
        inbox._state["floods"] = {}
        inbox._state["group_log"] = {}

    def test_a_flooding_group_becomes_one_question(self):
        now = 1000.0
        out = []
        for i, who in enumerate(["Sam", "Ali", "Sam", "Jo"]):
            out += inbox._route_groups([gtext(who)], now + i)
        self.assertEqual(len(out), 4, "a normal conversation is read as usual")
        ask = inbox._route_groups([gtext("Ali")], now + 5)
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0]["kind"], "flood")
        self.assertEqual(ask[0]["count"], 5)
        self.assertIn("ask the user", inbox._announcement(ask))
        self.assertEqual(inbox._route_groups([gtext("Sam"), gtext("Jo")], now + 6), [],
                         "after asking once, the flood is held silently")
        one_to_one = {"kind": "text", "name": "Mom", "chat": "", "chat_id": "m", "group": False,
                      "label": "Mom", "at": now, "text": "hi"}
        self.assertEqual(inbox._route_groups([one_to_one], now + 7), [one_to_one])
        later = inbox._route_groups([gtext("Sam")], now + 6 + inbox._FLOOD_QUIET)
        self.assertEqual([x["kind"] for x in later], ["text"], "calm for 10 minutes: back to normal")

    def test_several_at_once_flood_immediately(self):
        out = inbox._route_groups([gtext(n) for n in "ABCDEF"], 50.0)
        self.assertEqual([x["kind"] for x in out], ["flood"])
        self.assertIn("A, B, C and 3 others", out[0]["names"])

    def test_mute_after_a_flood_question_mutes_that_group(self):
        ask = inbox._route_groups([gtext(n) for n in "ABCDE"], 10.0)
        self.assertEqual(inbox._source(ask[0]), {"id": "g1", "label": "The Boys", "group": True})


class GroupSummary(unittest.TestCase):
    def setUp(self):
        from tests.test_messages_inbox import FakeMessages
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = FakeMessages(tmp.name)
        for p in (patch.object(imessage, "CHAT_DB", self.db.path),
                  patch.object(imessage, "contact_names", return_value={"4085550100": "Mom"})):
            p.start()
            self.addCleanup(p.stop)
        self.chat = self.db.group("chat999", "Baddies", [1, 2])
        inbox._state["floods"] = {}
        inbox._state["last_sources"] = []
        inbox._state["heard"] = []

    def test_summary_of_the_flooding_group(self):
        self.db.add("old news", handle=1, chat=self.chat, at=time.time() - 3 * 86400)
        for i in range(8):
            self.db.add(f"msg {i}", handle=1 + i % 2, chat=self.chat, at=time.time() - 100 + i)
        inbox._state["floods"]["chat999"] = {"label": "Baddies", "since": time.time() - 200,
                                             "count": 8, "names": [], "last": time.time()}
        out = inbox.run({"action": "summary"})
        self.assertIn("8 messages from Baddies", out)
        self.assertIn("Mom: msg 0", out)
        self.assertNotIn("old news", out)
        self.assertIn("about three sentences", out)
        self.assertEqual(inbox._state["floods"]["chat999"]["count"], 0, "next summary starts here")

    def test_what_did_i_miss_by_name(self):
        self.db.add("see you at 5", handle=1, chat=self.chat)
        out = inbox.run({"action": "summary", "chat": "what did I miss in baddies"})
        self.assertIn("see you at 5", out)
        self.assertIn("couldn't find", inbox.run({"action": "summary", "chat": "chess club"}))


class HeldWhileBusy(unittest.TestCase):
    def test_digest_counts_instead_of_reading(self):
        held = [gtext("A")] * 12 + [{"kind": "text", "name": "Mom", "label": "Mom", "group": False,
                                     "chat_id": "m", "text": "call me"}] * 2
        text = inbox._digest(held, "study mode")
        self.assertIn("2 texts from Mom", text)
        self.assertIn("12 messages in The Boys", text)
        self.assertNotIn("call me", text)


# ── do not disturb ───────────────────────────────────────────────────────────
class DoNotDisturb(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig(self, [dnd])

    def test_on_off_and_expiry(self):
        self.assertFalse(dnd.is_on())
        dnd.turn_on("study mode")
        self.assertEqual(dnd.reason(), "study mode")
        dnd.turn_off()
        self.assertFalse(dnd.is_on())
        dnd.turn_on("the night", until=time.time() + 60)
        self.assertTrue(dnd.is_on())
        self.assertFalse(dnd.is_on(now=time.time() + 120), "ends by itself in the morning")


# ── money ────────────────────────────────────────────────────────────────────
class Money(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(money_tracker, "DATA_FILE", Path(tmp.name) / "money.json")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(money_tracker, "_paper_bots", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def test_log_budget_and_summary(self):
        run = money_tracker.run
        self.assertIn("Set a $100 weekly food budget", run({"action": "budget", "category": "food", "amount": 100}))
        self.assertIn("$80 left", run({"action": "add", "amount": 20, "category": "lunch", "note": "Chipotle"}))
        self.assertIn("Heads up", run({"action": "add", "amount": 65, "category": "food"}))
        self.assertIn("over your weekly food budget", run({"action": "add", "amount": 30, "category": "groceries"}))
        run({"action": "add", "amount": 300, "kind": "income", "category": "job"})
        out = run({"action": "summary", "period": "week"})
        self.assertIn("You've spent $115 this week, mostly food $115", out)
        self.assertIn("brought in $300", out)
        self.assertIn("Over budget: food", out)
        self.assertIn("Removed $300", run({"action": "undo"}))
        self.assertEqual(run({"action": "add", "amount": "abc"}), "How much was it?")


# ── routines ─────────────────────────────────────────────────────────────────
class Routines(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig(self, [routines, dnd])
        self.music = patch.object(routines, "_music", return_value=True)
        self.music_mock = self.music.start()
        self.addCleanup(self.music.stop)
        for p in (patch.object(routines, "get_plugin_enabled", return_value=True),
                  patch.object(routines, "_canvas_url", return_value="https://sjsu.instructure.com/"),
                  patch.object(routines.subprocess, "run", return_value=MagicMock(returncode=0))):
            p.start()
            self.addCleanup(p.stop)

    def test_study_mode_and_back(self):
        from plugins import pomodoro
        with patch.object(pomodoro, "run", return_value="started") as pomo:
            out = routines.run({"action": "study", "task": "physics"})
            pomo.assert_called_once()
            self.assertEqual(pomo.call_args[0][0]["task"], "physics")
        self.assertTrue(dnd.is_on())
        self.assertIn("Study mode on", out)
        self.assertIn("focus music", out)
        self.assertIn("Canvas open", out)
        self.music_mock.assert_called_with("play", query="lofi focus study", kind="playlist")
        with patch.object(pomodoro, "run", return_value="stopped"):
            self.assertIn("Study mode off", routines.run({"action": "end_study"}))
        self.assertFalse(dnd.is_on())

    def test_goodnight(self):
        with patch.object(routines, "_tomorrow", return_value="Nothing is due tomorrow."), \
             patch("actions.reminder.reminder", return_value="Reminder set for x.") as rem, \
             patch.object(routines.threading, "Thread") as thread:
            out = routines.run({"action": "goodnight", "wake_time": "08:15"})
        self.assertEqual(rem.call_args[0][0]["time"], "08:15")
        self.assertIn("Wake-up reminder at 8:15 AM", out)
        self.assertTrue(dnd.is_on())
        thread.return_value.start.assert_called_once()       # the Mac locks after speaking
        self.music_mock.assert_called_with("pause")

    def test_next_wake_is_tomorrow_when_already_past(self):
        now = datetime(2026, 10, 15, 23, 0)
        self.assertEqual(routines._next_wake("07:30", now), datetime(2026, 10, 16, 7, 30))
        self.assertEqual(routines._next_wake("23:20", now), datetime(2026, 10, 16, 23, 20),
                         "less than 30 minutes away is treated as tomorrow")

    def test_custom_routine(self):
        out = routines.run({"action": "create", "name": "gym routine",
                            "steps": ["play my gym playlist", "start a 45 minute timer"]})
        self.assertIn("Say 'gym' to run it", out)
        run = routines.run({"action": "run", "name": "gym"})
        self.assertIn("1. play my gym playlist", run)
        self.assertIn("2. start a 45 minute timer", run)
        self.assertIn("gym", routines.run({"action": "list"}))
        self.assertIn("no routine called yoga", routines.run({"action": "run", "name": "yoga"}))


# ── YouTube Music ────────────────────────────────────────────────────────────
class Music(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.yt = MagicMock()
        for p in (patch.object(youtube_music, "_open", side_effect=self.opened.append),
                  patch.object(youtube_music, "_client", return_value=self.yt),
                  patch.object(youtube_music, "connected", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_song_artist_album_and_my_playlist(self):
        self.yt.get_library_playlists.return_value = [{"title": "Gym Bangers", "playlistId": "PLgym"}]
        self.assertIn("your playlist Gym Bangers", youtube_music.run({"action": "play", "query": "my gym bangers playlist"}))
        self.assertTrue(self.opened[-1].endswith("watch?list=PLgym"))

        self.yt.get_library_playlists.return_value = []
        self.yt.search.return_value = [{"title": "Blinding Lights", "videoId": "J7p", "artists": [{"name": "The Weeknd"}]}]
        self.assertEqual(youtube_music.run({"action": "play", "query": "blinding lights", "kind": "song"}),
                         "Playing Blinding Lights by The Weeknd.")
        self.assertTrue(self.opened[-1].endswith("watch?v=J7p"))

        self.yt.search.return_value = [{"browseId": "UCx", "artist": "Drake"}]
        self.yt.get_artist.return_value = {"name": "Drake", "shuffleId": "RDAOx"}
        self.assertIn("Shuffling Drake", youtube_music.run({"action": "play", "query": "drake", "kind": "artist"}))
        self.assertTrue(self.opened[-1].endswith("list=RDAOx"))

        self.assertEqual(youtube_music.run({"action": "play", "kind": "liked"}), "Playing your liked songs.")
        self.assertTrue(self.opened[-1].endswith("list=LM"))

    def test_public_playlist_drops_the_vl_prefix(self):
        self.yt.get_library_playlists.return_value = []
        self.yt.search.return_value = [{"title": "Lofi", "browseId": "VLPLabc"}]
        youtube_music.run({"action": "play", "query": "chill", "kind": "mood"})
        self.assertTrue(self.opened[-1].endswith("watch?list=PLabc"))

    def test_recent_plays_become_one_refreshed_playlist(self):
        cfg = {}
        self.yt.get_history.return_value = [{"videoId": "a"}, {"videoId": "b"}, {"videoId": "a"}]
        self.yt.create_playlist.return_value = "PLnew"
        with patch("memory.config_manager.load_api_keys", return_value={"youtube_music_recent_playlist": "PLold"}), \
             patch("memory.config_manager._save_flag", side_effect=cfg.__setitem__):
            out = youtube_music.run({"action": "play", "kind": "recent"})
        self.yt.delete_playlist.assert_called_once_with("PLold")
        self.assertEqual(self.yt.create_playlist.call_args.kwargs["video_ids"], ["a", "b"])
        self.assertEqual(cfg["youtube_music_recent_playlist"], "PLnew")
        self.assertIn("2 most recent songs", out)

    def test_recent_arabic_song_plays_as_a_radio(self):
        self.yt.get_history.return_value = [
            {"videoId": "en1", "title": "Blinding Lights", "artists": [{"name": "The Weeknd"}]},
            {"videoId": "ar1", "title": "تملي معاك", "artists": [{"name": "Amr Diab"}]},
            {"videoId": "ar2", "title": "Nour El Ein", "artists": [{"name": "Amr Diab"}]},
        ]
        with patch("random.choice", side_effect=lambda items: items[-1]) as pick:
            out = youtube_music.run({"action": "play", "kind": "recent", "query": "arabic music"})
        # The romanised title counts too: the same artist has an Arabic-script song.
        self.assertEqual([i["videoId"] for i in pick.call_args.args[0]], ["ar1", "ar2"])
        self.assertTrue(self.opened[-1].endswith("watch?v=ar2&list=RDAMVMar2"))
        self.assertEqual(out, "Playing Nour El Ein by Amr Diab, then similar songs.")

    def test_recent_with_no_match_falls_back_to_a_mix(self):
        self.yt.get_history.return_value = [{"videoId": "en1", "title": "Blinding Lights",
                                             "artists": [{"name": "The Weeknd"}]}]
        self.yt.search.return_value = [{"title": "Arabic Hits", "browseId": "VLPLar"}]
        out = youtube_music.run({"action": "play", "kind": "recent", "query": "arabic"})
        self.assertIn("didn't find any arabic songs in your recent plays", out)
        self.assertTrue(self.opened[-1].endswith("watch?list=PLar"))
        self.yt.get_history.return_value = [{"videoId": "x", "title": "Song", "artists": [{"name": "Drake"}]}]
        youtube_music.run({"action": "play", "kind": "recent", "query": "drake"})
        self.assertTrue(self.opened[-1].endswith("watch?v=x&list=RDAMVMx"))

    def test_chrome_split_line_headers_are_cleaned(self):
        # Chrome's newer "Copy request headers": names and values on separate
        # lines, pseudo-headers first, and a multi-line Decoded block.
        raw = "\n".join([
            ":authority", "music.youtube.com", ":path", "/youtubei/v1/browse?x=1",
            "content-encoding", "gzip", "content-length", "2055", "cookie", "SID=abc; HSID=def",
            "sec-fetch-mode", "same-origin", "x-client-data", "CJa2",
            "Decoded:", "message ClientVariations {", "  repeated int32 x = [1, 2];", "}",
            "x-goog-authuser", "3", "user-agent", "Mozilla/5.0 (Macintosh)",
        ])
        self.assertEqual(youtube_music._clean_headers(raw).split("\n"), [
            "cookie: SID=abc; HSID=def", "x-client-data: CJa2", "x-goog-authuser: 3",
            "user-agent: Mozilla/5.0 (Macintosh)"])
        classic = "Cookie: SID=abc\r\nX-Goog-AuthUser: 0\r\nContent-Encoding: gzip\r\nHost: music.youtube.com"
        self.assertEqual(youtube_music._clean_headers(classic), "cookie: SID=abc\nx-goog-authuser: 0")

    def test_controls_use_the_right_media_key(self):
        with patch.object(youtube_music, "_media_key", return_value=True) as key:
            self.assertEqual(youtube_music.run({"action": "next"}), "Skipped.")
            key.assert_called_with("next")
            self.assertEqual(youtube_music.run({"action": "previous"}), "Going back.")
            key.assert_called_with("previous")
            youtube_music.run({"action": "pause"})
            key.assert_called_with("play")

    def test_account_features_explain_how_to_connect(self):
        with patch.object(youtube_music, "_client", side_effect=youtube_music._NeedsConnect):
            self.assertIn("connect YouTube Music", youtube_music.run({"action": "playlists"}))

    def test_connect_done_needs_the_copied_headers(self):
        with patch.object(youtube_music.subprocess, "run", return_value=MagicMock(stdout="hello")):
            self.assertIn("don't see the copied headers", youtube_music._connect_done())


# ── reopen reminders ─────────────────────────────────────────────────────────
class StartupReminders(unittest.TestCase):
    def test_ongoing_missions_and_reminders_are_spoken(self):
        now = datetime(2026, 10, 15, 9, 0)
        tasks = [{"id": 1, "title": "Sell PS5 on eBay", "due": "", "status": "open"},
                 {"id": 2, "title": "Return library book", "due": "", "status": "open"},
                 {"id": 3, "title": "Lab report", "due": "2026-10-15", "status": "open"}]
        rem = [(now + timedelta(hours=9), "Coach at Ponderosa Park"),
               (now + timedelta(days=40), "Far away")]
        spoken, panel = sb.compose(tasks, None, None, "2026-10-15", reminders=rem, now=now)
        self.assertIn("1 due today: Lab report", spoken)
        self.assertIn("also on the list: Sell PS5 on eBay; Return library book", spoken)
        self.assertIn("today 6:00 PM: Coach at Ponderosa Park", spoken)
        self.assertNotIn("Far away", spoken)
        self.assertIn("Far away", panel)

    def test_pending_reminders_reads_launch_agents(self):
        from actions import reminder
        with tempfile.TemporaryDirectory() as d:
            agents, scripts = Path(d) / "agents", Path(d) / "scripts"
            agents.mkdir()
            scripts.mkdir()
            for stamp, msg in (("20991231_090000", "Future one"), ("20000101_090000", "Past one")):
                (agents / f"com.jarvis.reminder.JARVISReminder_{stamp}.plist").write_text("x")
                (scripts / f"JARVISReminder_{stamp}.py").write_text(f"x = 1\nmessage = {json.dumps(msg)}\n")
            with patch.object(reminder, "_scripts_dir", return_value=scripts):
                got = reminder.pending_reminders(agents)
        self.assertEqual([m for _, m in got], ["Future one"])


class MissionDuplicates(unittest.TestCase):
    def test_number_words_match_digits(self):
        self.assertTrue(mission_control._same_title("Physics Test 2 Exam", "physics test two exam"))
        self.assertFalse(mission_control._same_title("Physics Test 2", "Physics Test 3"))


if __name__ == "__main__":
    unittest.main()
