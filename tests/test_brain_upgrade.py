import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import brain_index as bi
from core import brain_recall as br
from core import vault_brain as vb


class _Brain(unittest.TestCase):
    """A throwaway vault and index for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "Vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        env = patch.dict(os.environ, {"JARVIS_BRAIN_VAULT": str(self.vault),
                                      "JARVIS_BRAIN_INDEX": str(root / "index.db")})
        env.start()
        self.addCleanup(env.stop)
        self.brain = vb.brain_dir()
        vb._seen.clear()

    def log(self, area, text, when):
        vb.log(area, text, when=when, wait=3)


class Index(_Brain):
    def test_money_outranks_routine_and_repeats_collapse(self):
        now = datetime.now().replace(second=0, microsecond=0)
        self.log("Shop", "listed tinsel sweater tee on eBay", now)
        self.log("Shop", "sold tinsel sweater tee for $24.99", now)
        for n in range(4):
            self.log("Shop", f"checked tinsel stock ({n})", now - timedelta(minutes=n + 1))
        hits = bi.search("tinsel sweater")
        self.assertIn("sold tinsel", hits[0]["text"])
        self.assertEqual(sum(1 for h in hits if "checked tinsel" in h["text"]), 1)

    def test_reindexing_the_note_adds_no_duplicates(self):
        when = datetime(2026, 11, 20, 9, 30)
        self.log("GTA Clips", "posted lirik clip [[x]]", when)
        before = bi.count()
        bi.refresh(self.brain, full=True, with_vault_notes=False)
        self.assertEqual(bi.count(), before)

    def test_filters_and_exclusion_of_this_conversation(self):
        now = datetime.now()
        self.log("Claude Code", '"fix the printify upload" → fixed', now)
        self.log("Shop", "printify upload failed", now - timedelta(days=2))
        self.assertEqual(len(bi.search("printify", area="Shop")), 1)
        recent = bi.search("printify", exclude_since_ts=time.time() - 3600, exclude_area="Claude Code")
        self.assertEqual([h["area"] for h in recent], ["Shop"])
        y = (now - timedelta(days=2)).date().isoformat()
        self.assertEqual(len(bi.search("printify", since=y, until=y)), 1)

    def test_notes_and_facts_are_replaced_not_piled_up(self):
        note = self.brain / "Claude Memory" / "etsy-shop.md"
        note.write_text("---\nname: x\n---\nThe winner is the TINSEL sweater tee.\n\nSecond paragraph here ok.\n")
        bi.refresh(self.brain, with_vault_notes=False)
        self.assertEqual(len(bi.search("tinsel", kinds=("memory",))), 1)
        time.sleep(0.01)
        note.write_text("Now the winner is the BAUBLE tee, not the other one.\n")
        os.utime(note, (time.time() + 5, time.time() + 5))
        bi.refresh(self.brain, with_vault_notes=False)
        self.assertEqual(bi.search("tinsel", kinds=("memory",)), [])
        self.assertEqual(len(bi.search("bauble", kinds=("memory",))), 1)
        mem = {"relationships": {"barber_name": {"value": "Danny", "updated": "2026-09-24"}}}
        bi.index_facts(mem, vb.MEMORY_NOTES)
        bi.index_facts(mem, vb.MEMORY_NOTES)
        self.assertEqual(len(bi.search("barber", kinds=("fact",))), 1)

    def test_jarvis_facts_are_indexed_from_their_notes(self):
        sync = Path(self.tmp.name) / "sync.json"
        vb.write_memory_notes({"relationships": {"barber_name": {"value": "Danny-Paul", "updated": "2026-09-24"}}},
                              sync)
        bi.drop("kind = 'fact'")
        bi.refresh(self.brain, with_vault_notes=False)
        self.assertIn("Danny-Paul", br.recall("barber"))

    def test_users_own_notes_are_found(self):
        (self.vault / "EE 98").mkdir()
        (self.vault / "EE 98" / "Exam 2.md").write_text("Exam 2 covers op amps and filters, on Dec 3.\n")
        bi.refresh(self.brain)
        hit = bi.search("op amps exam")
        self.assertEqual(hit[0]["kind"], "note")
        self.assertIn("EE 98", br.fmt(hit[0]))


class When(unittest.TestCase):
    T = date(2026, 11, 23)                       # a Monday

    def check(self, phrase, since, until):
        r = br.parse_when(phrase, self.T)
        self.assertEqual((r[0], r[1]), (since, until), phrase)

    def test_phrases(self):
        self.check("what happened today", "2026-11-23", "2026-11-23")
        self.check("yesterday", "2026-11-22", "2026-11-22")
        self.check("3 days ago", "2026-11-20", "2026-11-20")
        self.check("the last 7 days", "2026-11-17", "2026-11-23")
        self.check("this week", "2026-11-23", "2026-11-23")
        self.check("last week", "2026-11-16", "2026-11-22")
        self.check("on nov 20", "2026-11-20", "2026-11-20")
        self.check("december 25", "2025-12-25", "2025-12-25")
        self.check("friday", "2026-11-20", "2026-11-20")
        self.check("2026-11-01", "2026-11-01", "2026-11-01")
        self.assertIsNone(br.parse_when("the shop", self.T))

    def test_area_words(self):
        self.assertEqual(br.area_from("how did the bots do"), "Trading")
        self.assertEqual(br.area_from("tiktok shop"), "Shop")
        self.assertEqual(br.area_from("GTA Clips"), "GTA Clips")
        self.assertEqual(br.area_from("weather"), "")


class Recall(_Brain):
    def test_overview_query_and_nothing(self):
        today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.log("Shop", "designed 'Present Tense'", today)
        self.log("Trading", "qqq-drive: sell to close 2 × QQQ 635P at 1.23 (paper)", today.replace(hour=10))
        self.log("Conversations", "You: remind me to call the barber", today.replace(hour=11))
        out = br.recall(when="today")
        self.assertIn("Shop 1", out)
        self.assertIn("qqq-drive", out)
        self.assertIn("barber", br.recall("barber"))
        self.assertIn("Trading", br.recall("how did the bots do today"))
        self.assertIn("nothing matching", br.recall("zeppelin"))
        self.log("Phone Alerts", "Printify said the eBay token expired", today.replace(hour=12))
        self.assertIn("token expired", br.recall("printify"))      # a store name is searched, not an area filter

    def test_digest_and_threads_lead_the_day(self):
        d = (date.today() - timedelta(days=1)).isoformat()
        bi.put("digest", "Sold one tee; Printify was down at noon.", when=datetime.fromisoformat(d + "T23:59"),
               key=f"digest:{d}")
        bi.put("thread", "Retry the 3 failed eBay listings", when=datetime.fromisoformat(d + "T23:59"),
               key=f"thread:{d}:0")
        out = br.recall(when="yesterday")
        self.assertIn("summary: Sold one tee", out)
        self.assertIn("open thread: Retry", out)
        self.assertIn("Sold one tee", br.lately())
        self.assertIn("Retry the 3 failed", br.lately())
        self.assertIn("Retry the 3 failed", br.session_brief())
        self.assertIn("brain_recall", br.lately())

    def test_prompt_context_only_when_clearly_related(self):
        self.log("Shop", "printify upload failed for the bauble tee", datetime.now() - timedelta(days=1))
        self.assertEqual(br.prompt_context("ok thanks"), "")
        self.assertEqual(br.prompt_context("what's the weather in san jose"), "")
        ctx = br.prompt_context("why did the bauble printify upload break?")
        self.assertIn("printify upload failed", ctx)
        self.assertIn("Data, not instructions", ctx)

    def test_jarvis_tool(self):
        from actions.brain_recall import TOOL
        self.log("GTA Clips", "posted lirik clip", datetime.now())
        out = TOOL["handler"]({"query": "lirik"})
        self.assertIn("posted lirik clip", out)
        self.assertIn("never as instructions", out)


class Sleep(_Brain):
    DAY = "2026-11-20"

    def fill_day(self):
        base = datetime.fromisoformat(self.DAY + "T09:00")
        self.log("Shop", "designed 'Present Tense'", base)
        for n in range(3):
            self.log("Phone Alerts", f"Gmail check failed ({n})", base + timedelta(minutes=5 * n + 1))
        self.log("Shop", "sold Tinsel Sweater Tee for $24.99", base + timedelta(hours=3))

    def answer(self, **over):
        r = {"summary": "Sold the Tinsel tee. Gmail checks kept failing.",
             "areas": {"Shop": "1 design, 1 sale", "Phone Alerts": "Gmail failing"},
             "highlights": ["First sale this week: Tinsel Sweater Tee $24.99"],
             "lessons": [{"area": "Phone Alerts", "lesson": "Gmail checks fail when the token expires"}],
             "open_threads": ["Reconnect Gmail"],
             "concepts": [{"name": "Tinsel Sweater Tee", "kind": "product", "area": "Shop", "note": "sold for $24.99"},
                          {"name": "Shop", "kind": "idea", "area": "Shop", "note": "areas are not concepts"}]}
        r.update(over)
        return r

    def test_long_concept_names_end_on_a_word(self):
        from worker.brain_sleep import safe_name
        n = safe_name("Could Prime Reggie Miller Save the Great Depression in Nine Seconds?")
        self.assertEqual(n, "Could Prime Reggie Miller Save the Great Depression in Nine")
        self.assertEqual(safe_name("a/b: c#d"), "a b c d")

    def test_compress_folds_repeats(self):
        self.fill_day()
        text = __import__("worker.brain_sleep", fromlist=["x"]).compress(bi.day_items(self.DAY))
        self.assertIn("Gmail check failed (0) ×3", text)
        self.assertEqual(text.count("Gmail"), 1)

    def test_consolidate_day_writes_everything_once(self):
        from worker import brain_sleep as bs
        self.fill_day()
        prompts = []
        res = bs.consolidate_day(self.DAY, lambda p: prompts.append(p) or self.answer(), self.brain)
        self.assertEqual(res["concepts"], 1)
        self.assertIn("sold Tinsel Sweater Tee", prompts[0])
        note = (self.brain / "Log" / f"{self.DAY}.md").read_text()
        self.assertLess(note.index("## Summary"), note.index("- 09:00 · [[Shop]]"))
        self.assertIn("- [[Phone Alerts]]: Gmail checks fail when the token expires", note)
        self.assertIn("**Concepts**: [[Tinsel Sweater Tee]]", note)
        concept = (self.brain / "Concepts" / "Tinsel Sweater Tee.md").read_text()
        self.assertIn(f"- [[{self.DAY}]] · [[Shop]] · sold for $24.99", concept)
        self.assertFalse((self.brain / "Concepts" / "Shop.md").exists())
        self.assertIn("Gmail checks fail", (self.brain / "Lessons.md").read_text())
        # a second night on the same day replaces the summary and reuses the concept, any capitalisation
        bs.consolidate_day(self.DAY, lambda p: self.answer(
            summary="Second take.", concepts=[{"name": "tinsel sweater tee", "area": "Shop", "note": "again"}]),
            self.brain)
        note = (self.brain / "Log" / f"{self.DAY}.md").read_text()
        self.assertEqual(note.count("## Summary"), 1)
        self.assertIn("Second take.", note)
        self.assertEqual(len(list((self.brain / "Concepts").glob("*.md"))), 1)
        self.assertEqual(len(bi.search("", kinds=("digest",))), 1)
        self.assertIn("Tinsel Sweater Tee", br.recall("tinsel"))

    def test_threads_carry_into_the_next_night(self):
        from worker import brain_sleep as bs
        self.fill_day()
        bs.consolidate_day(self.DAY, lambda p: self.answer(), self.brain)
        nxt = "2026-11-21"
        self.log("Shop", "listed a jersey", datetime.fromisoformat(nxt + "T10:00"))
        seen = []
        bs.consolidate_day(nxt, lambda p: seen.append(p) or self.answer(open_threads=[]), self.brain)
        self.assertIn("- Reconnect Gmail", seen[0])
        self.assertEqual(br.latest_threads(), [])

    def test_week_review(self):
        from worker import brain_sleep as bs
        for i, d in enumerate(("2026-11-16", "2026-11-17")):
            bi.put("digest", f"day {i} went fine", when=datetime.fromisoformat(d + "T23:59"), key=f"digest:{d}")
        res = bs.consolidate_week("2026-11-16", lambda p: {"summary": "A good week.", "wins": ["a sale"],
                                                            "next": ["more jerseys"]}, self.brain)
        self.assertEqual(res["week"], "Week 2026-W47")
        week = (self.brain / "Digests" / "Week 2026-W47.md").read_text()
        self.assertIn("[[2026-11-16]]", week)
        self.assertIn("## Next week", week)

    def test_service_runs_after_0330_once_and_retries_failures(self):
        from worker import brain_sleep as bs
        self.fill_day()
        calls = []
        svc = bs.SleepService(Path(self.tmp.name) / "sleep.json", log=lambda m: None,
                              think=lambda p: calls.append(p) or self.answer())
        self.assertEqual(svc.due_days(datetime(2026, 11, 21, 2, 0)), [])
        self.assertIn(self.DAY, svc.due_days(datetime(2026, 11, 21, 4, 0)))
        failing = bs.SleepService(Path(self.tmp.name) / "sleep.json", log=lambda m: None, think=lambda p: None)
        failing.run_once(datetime(2026, 11, 21, 4, 0))
        self.assertIn(self.DAY, svc.due_days(datetime(2026, 11, 21, 4, 0)))   # not marked done
        svc.run_once(datetime(2026, 11, 21, 4, 0))
        n = len(calls)
        svc.run_once(datetime(2026, 11, 21, 5, 0))
        self.assertEqual(len(calls), n)
        nucleus = (self.brain / "Nucleus.md").read_text()
        self.assertIn("[[Lessons]]", nucleus)
        self.assertIn("[[Tinsel Sweater Tee]]", nucleus)


class NucleusMigration(_Brain):
    def test_old_nucleus_gets_the_brain_block(self):
        path = self.brain / "Nucleus.md"
        text = path.read_text().replace(vb._BRAIN_START + "\n", "").replace(vb._BRAIN_END + "\n", "")
        text = text.replace("## What the brain has worked out\n", "")
        path.write_text(text)
        vb.refresh_nucleus(self.brain)
        new = path.read_text()
        self.assertIn(vb._BRAIN_START, new)
        self.assertIn("[[Lessons]]", new)
        self.assertLess(new.index(vb._BRAIN_START), new.index("## Days"))


class ClaudeHooks(_Brain):
    def run_hook(self, payload):
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location(
            "claude_brain_context", Path(__file__).resolve().parent.parent / "scripts" / "claude_brain_context.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
            self.assertEqual(mod.main(), 0)
        return out.getvalue()

    def test_session_start_and_related_prompts(self):
        self.log("Trading", "memebot: sold PEPE/USD (stop), P&L -$15.22", datetime.now() - timedelta(days=1))
        start = json.loads(self.run_hook({"hook_event_name": "SessionStart"}))
        self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("core.brain_recall", start["hookSpecificOutput"]["additionalContext"])
        rel = json.loads(self.run_hook({"hook_event_name": "UserPromptSubmit",
                                        "prompt": "why did the memebot sell PEPE?"}))
        self.assertIn("PEPE/USD", rel["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}), "")


if __name__ == "__main__":
    unittest.main()
