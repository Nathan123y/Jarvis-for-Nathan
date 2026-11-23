import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core import vault_brain as vb


class _Vault(unittest.TestCase):
    """A throwaway vault that vault_brain writes into (never the real one)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "Vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        env = patch.dict(os.environ, {"JARVIS_BRAIN_VAULT": str(self.vault)})
        env.start()
        self.addCleanup(env.stop)
        self.brain = self.vault / vb.BRAIN
        vb._seen.clear()

    def day_note(self, day="2026-11-23"):
        return (self.brain / "Log" / f"{day}.md").read_text(encoding="utf-8")


class NoVault(unittest.TestCase):
    def test_test_runs_never_find_the_real_vault(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_BRAIN_VAULT", None)
            self.assertIsNone(vb.vault_root())
            vb.log("Shop", "goes nowhere", wait=2)          # no error, nothing written


class Skeleton(_Vault):
    def test_nucleus_and_area_hubs_link_together(self):
        (self.vault / "EE 98").mkdir()
        (self.vault / "EE 98" / "EE 98.md").write_text("class", encoding="utf-8")
        (self.vault / "EE 98" / "HW2.md").write_text("hw", encoding="utf-8")
        (self.vault / "SJSU.md").write_text("school", encoding="utf-8")
        brain = vb.brain_dir()
        nucleus = (brain / "Nucleus.md").read_text(encoding="utf-8")
        for title in vb.AREAS:
            self.assertIn(f"[[{title}]]", nucleus)
            hub = (brain / "Areas" / f"{vb._file_name(title)}.md").read_text(encoding="utf-8")
            self.assertIn("[[Nucleus]]", hub)
        self.assertIn("[[Me - People]]", nucleus)
        self.assertIn("[[MEMORY]]", nucleus)
        self.assertIn("**EE 98**: [[EE 98]]", nucleus)     # folder with a same-name note links that one
        self.assertIn("- [[SJSU]]", nucleus)
        shop = (brain / "Areas" / "Shop.md").read_text(encoding="utf-8")
        self.assertIn("[[etsy-shop]]", shop)

    def test_existing_notes_are_never_overwritten(self):
        (self.brain / "Areas").mkdir(parents=True)
        (self.brain / "Nucleus.md").write_text("my own nucleus", encoding="utf-8")
        (self.brain / "Areas" / "Shop.md").write_text("my shop notes", encoding="utf-8")
        vb.ensure_skeleton(self.brain)
        self.assertEqual((self.brain / "Nucleus.md").read_text(encoding="utf-8"), "my own nucleus")
        self.assertEqual((self.brain / "Areas" / "Shop.md").read_text(encoding="utf-8"), "my shop notes")


class DailyLog(_Vault):
    def test_lines_go_to_that_days_note_with_area_links(self):
        when = datetime(2026, 11, 23, 13, 19)
        vb.log("Shop", "designed 'Present Tense'", when=when, wait=3)
        vb.log("Claude Code", '"fix it" → fixed', links=("Shop", "Claude Code"), when=when, wait=3)
        text = self.day_note()
        self.assertTrue(text.startswith("---\ntags: [jarvis-log]"))
        self.assertIn("# Monday, November 23, 2026", text)
        self.assertIn("Part of [[Nucleus]]", text)
        self.assertIn("- 13:19 · [[Shop]] · designed 'Present Tense'\n", text)
        self.assertIn('- 13:19 · [[Claude Code]] · "fix it" → fixed → [[Shop]]\n', text)
        self.assertEqual(text.count("# Monday"), 1)
        nucleus = (self.brain / "Nucleus.md").read_text(encoding="utf-8")
        self.assertIn("- [[2026-11-23]]", nucleus)

    def test_next_day_links_the_day_before(self):
        vb.log("Shop", "a", when=datetime(2026, 11, 22, 9, 0), wait=3)
        vb.log("Shop", "b", when=datetime(2026, 11, 23, 9, 0), wait=3)
        self.assertIn("day before: [[2026-11-22]]", self.day_note("2026-11-23"))

    def test_repeats_are_written_once_when_deduped(self):
        when = datetime(2026, 11, 23, 13, 0)
        for n in range(5):
            vb.log("Phone Alerts", f"Gmail check failed ({n} in a row)", dedupe=True, when=when)
        vb.log("Conversations", "You: hi", when=when)
        vb.log("Conversations", "You: hi", when=when, wait=3)
        text = self.day_note()
        self.assertEqual(text.count("Gmail check failed"), 1)
        self.assertEqual(text.count("You: hi"), 2)          # conversation is never deduped

    def test_clean_hides_secrets_and_defuses_links(self):
        s = vb.clean("api_key=abc123 token: zzz Bearer abcdefghijkl sk-ABCDEFGHIJKLMNOPQRS [[x]]\nnext")
        self.assertNotIn("abc123", s)
        self.assertNotIn("zzz", s)
        self.assertNotIn("abcdefghijkl", s)
        self.assertNotIn("sk-ABCDEF", s)
        self.assertNotIn("[[", s)
        self.assertNotIn("\n", s)
        self.assertTrue(len(vb.clean("x" * 5000)) <= vb.LINE_MAX)

    def test_worker_log_handler_files_lines_by_area(self):
        logger = logging.getLogger("jarvis.test_vault_brain_shop")
        logger.propagate = False
        h = vb.VaultLogHandler()
        logger.addHandler(h)
        self.addCleanup(logger.removeHandler, h)
        logger.setLevel(logging.INFO)
        with patch.dict(vb._LOGGER_AREAS, {"jarvis.test_vault_brain_shop": "Shop"}):
            logger.info("shop: designed 'X'")
            logger.info("jerseys: listed 'Y'")
            logger.warning("printify down")
            vb.flush(3)
        text = self.day_note(datetime.now().date().isoformat())
        self.assertIn("[[Shop]] · shop: designed 'X'", text)
        self.assertIn("[[Jersey Line]] · jerseys: listed 'Y'", text)
        self.assertIn("[[Shop]] · warning: printify down", text)


class Mapping(unittest.TestCase):
    def test_logger_and_prefix(self):
        self.assertEqual(vb.area_for_logger("jarvis.shop", "storefront: published 192"), "Shop")
        self.assertEqual(vb.area_for_logger("jarvis.shop", "jerseys: listed"), "Jersey Line")
        self.assertEqual(vb.area_for_logger("jarvis.clips", "scout: LIVE moment"), "GTA Clips")
        self.assertEqual(vb.area_for_logger("jarvis.alerts", "phone alert sent: x"), "Phone Alerts")
        self.assertEqual(vb.area_for_logger("jarvis.worker", "worker started pid=1"), "Worker")
        self.assertEqual(vb.area_for_logger("jarvis.other", "hello"), "Worker")

    def test_tools_and_paths(self):
        self.assertEqual(vb.area_for_tool("day_trading"), "Trading")
        self.assertEqual(vb.area_for_tool("etsy_shop"), "Shop")
        self.assertEqual(vb.area_for_tool("open_app"), "Conversations")
        self.assertEqual(vb.areas_for_paths(["/r/worker/jersey_shop.py", "/r/worker/shop.py", "/r/main.py"]),
                         ["Jersey Line", "Shop"])


class MemoryNotes(_Vault):
    def setUp(self):
        super().setUp()
        self.sync = Path(self.tmp.name) / "vault_sync.json"
        self.memory = {
            "identity": {"name": {"value": "Nathan", "updated": "2026-09-22"}},
            "relationships": {"barber_name": {"value": "Danny", "updated": "2026-09-24"},
                              "old_style": "a bare string"},
            "preferences": {}, "projects": {}, "wishes": {}, "notes": {},
            "sessions": [{"date": "2026-11-06", "summary": "x"}],
        }
        vb.write_memory_notes(self.memory, self.sync)
        self.people = self.brain / "Jarvis Memory" / "Me - People.md"

    def test_round_trip_and_no_change_without_an_edit(self):
        text = self.people.read_text(encoding="utf-8")
        self.assertIn("- **barber_name**: Danny `2026-09-24`", text)
        self.assertIn("[[Nucleus]]", text)
        parsed = vb.parse_memory_note(text)
        self.assertEqual(parsed["barber_name"], {"value": "Danny", "updated": "2026-09-24"})
        self.assertEqual(vb.adopt_memory_edits(self.memory, self.sync), [])

    def _edit(self, fn):
        text = fn(self.people.read_text(encoding="utf-8"))
        time.sleep(0.01)
        self.people.write_text(text, encoding="utf-8")

    def test_an_edit_in_obsidian_is_taken_in(self):
        self._edit(lambda t: t.replace("Danny `2026-09-24`", "Danny-Paul `2026-09-24`")
                   + "- **sister**: Ayse\n")
        mem = json.loads(json.dumps(self.memory))
        self.assertEqual(vb.adopt_memory_edits(mem, self.sync), ["relationships"])
        self.assertEqual(mem["relationships"]["barber_name"]["value"], "Danny-Paul")
        self.assertEqual(mem["relationships"]["sister"]["value"], "Ayse")
        self.assertEqual(mem["identity"], self.memory["identity"])      # other notes untouched
        self.assertEqual(vb.adopt_memory_edits(mem, self.sync), [])     # taken once

    def test_deleting_a_line_forgets_it(self):
        self._edit(lambda t: "\n".join(l for l in t.splitlines() if "barber_name" not in l) + "\n")
        mem = json.loads(json.dumps(self.memory))
        vb.adopt_memory_edits(mem, self.sync)
        self.assertNotIn("barber_name", mem["relationships"])

    def test_missing_note_or_marker_changes_nothing(self):
        self.people.unlink()
        mem = json.loads(json.dumps(self.memory))
        self.assertEqual(vb.adopt_memory_edits(mem, self.sync), [])
        self.people.write_text("# Me - People\n- **x**: y\n", encoding="utf-8")
        self.assertEqual(vb.adopt_memory_edits(mem, self.sync), [])
        self.assertEqual(mem["relationships"], self.memory["relationships"])


class MemoryManagerUsesTheVault(_Vault):
    def test_save_writes_notes_and_obsidian_edits_come_back(self):
        from memory import memory_manager as mm
        mem_path = Path(self.tmp.name) / "long_term.json"
        sync = Path(self.tmp.name) / "vault_sync.json"
        with patch.object(mm, "MEMORY_PATH", mem_path), patch.object(mm, "VAULT_SYNC_PATH", sync):
            mm.update_memory({"relationships": {"barber_name": {"value": "Danny"}}})
            vb.flush(3)
            note = self.brain / "Jarvis Memory" / "Me - People.md"
            self.assertIn("**barber_name**: Danny", note.read_text(encoding="utf-8"))
            log = self.day_note(datetime.now().date().isoformat())
            self.assertIn("[[Jarvis Memory]] · remembered barber name: Danny → [[Me - People]]", log)
            time.sleep(0.01)
            note.write_text(note.read_text(encoding="utf-8").replace("Danny", "Danny-Paul"), encoding="utf-8")
            self.assertEqual(mm.load_memory()["relationships"]["barber_name"]["value"], "Danny-Paul")
            saved = json.loads(mem_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["relationships"]["barber_name"]["value"], "Danny-Paul")


class TradingFeed(_Vault):
    def test_new_fills_and_meme_trades_are_logged_once(self):
        from worker.brain_feed import TradingFeed as Feed
        bots = Path(self.tmp.name) / "optionsbots"
        (bots / "data" / "native").mkdir(parents=True)
        (bots / "data" / "memebot").mkdir(parents=True)
        db = bots / "data" / "native" / "paper.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE fills (seq INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, side TEXT, "
                    "contract_json TEXT, quantity INTEGER, price TEXT, ts TEXT)")
        row = ("qqq-drive", "SELL_TO_CLOSE",
               json.dumps({"underlying": "QQQ", "strike": "635", "right": "P", "expiration": "2026-11-24"}),
               2, "1.23", "2026-11-23T20:52:46Z")
        con.execute("INSERT INTO fills (bot_id, side, contract_json, quantity, price, ts) VALUES (?,?,?,?,?,?)", row)
        con.commit()
        trades = bots / "data" / "memebot" / "trades.jsonl"
        trades.write_text(json.dumps({"ts": "2026-11-20T10:00:00+00:00", "kind": "buy", "symbol": "OLD"}) + "\n")
        lines = []
        feed = Feed(Path(self.tmp.name) / "feed.json", bots, log=lambda a, t, **k: lines.append((a, t)))
        feed.poll()                                   # first run starts at the end
        self.assertEqual(lines, [])
        con.execute("INSERT INTO fills (bot_id, side, contract_json, quantity, price, ts) VALUES (?,?,?,?,?,?)", row)
        con.commit()
        con.close()
        with open(trades, "a") as f:
            f.write(json.dumps({"ts": "2026-11-23T21:00:00+00:00", "kind": "sell", "symbol": "PEPE/USD",
                                "why": "stop", "pnl": -15.22}) + "\n")
        feed.poll()
        feed.poll()
        self.assertEqual(lines, [
            ("Trading", "qqq-drive: sell to close 2 × QQQ 635P 2026-11-24 at 1.23 (paper)"),
            ("Trading", "memebot: sold PEPE/USD (stop), P&L -$15.22"),
        ])


class ClaudeHook(_Vault):
    def records(self):
        return [
            {"type": "user", "message": {"content": "old request"}, "origin": {"kind": "human"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "old reply"}]}},
            {"type": "user", "message": {"content": "<system-reminder>ctx</system-reminder>make the shop faster"},
             "origin": {"kind": "human"}, "turnOrigin": "human"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/worker/shop.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -F m.txt && git push"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "skill text"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done: **shop** is faster."}]}},
        ]

    def test_summarizes_the_last_turn_and_logs_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "claude_vault_log", Path(__file__).resolve().parent.parent / "scripts" / "claude_vault_log.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        turn = hook.summarize_turn(self.records())
        self.assertEqual(turn["prompt"], "make the shop faster")
        self.assertEqual(turn["edited"], ["/repo/worker/shop.py"])
        self.assertTrue(turn["committed"] and turn["pushed"])
        line = hook.turn_line(turn, "/repo")
        self.assertEqual(line, '"make the shop faster" → Done: shop is faster. · edited worker/shop.py · committed + pushed')

        transcript = Path(self.tmp.name) / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in self.records()) + "\n")
        stdin = json.dumps({"transcript_path": str(transcript), "cwd": "/repo"})
        with patch("sys.stdin") as fake_in:
            fake_in.read.return_value = stdin
            self.assertEqual(hook.main(), 0)
        text = self.day_note(datetime.now().date().isoformat())
        self.assertIn('[[Claude Code]] · "make the shop faster"', text)
        self.assertIn("→ [[Shop]]", text)


if __name__ == "__main__":
    unittest.main()
