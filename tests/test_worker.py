import json
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from worker import jobs as J, launchd
from worker.jobs import JobDB, backoff
from worker.runtime import (Deferred, Fatal, NeedsSetup, Worker, KeepAwake, keep_awake_allowed, parse_power,
                            default_handlers)

REPO = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "worker"
        self.db = JobDB(self.dir / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()


class QueueTests(Base):
    def test_claim_order_and_states(self):
        a = self.db.enqueue("x", priority=5, now=100)
        b = self.db.enqueue("x", priority=1, now=100)
        job = self.db.claim("w", now=101)
        self.assertEqual((job["id"], job["state"], job["attempts"]), (b, "running", 1))
        self.assertEqual(self.db.claim("w", now=101)["id"], a)
        self.assertIsNone(self.db.claim("w", now=101))

    def test_run_after_is_respected(self):
        self.db.enqueue("x", run_after=500, now=100)
        self.assertIsNone(self.db.claim("w", now=100))
        self.assertIsNotNone(self.db.claim("w", now=500))

    def test_unique_key_dedupes(self):
        a = self.db.enqueue("x", unique_key="k")
        self.assertEqual(self.db.enqueue("x", unique_key="k"), a)
        self.assertEqual(len(self.db.list()), 1)

    def test_retry_backoff_then_fail(self):
        self.db.enqueue("x", max_attempts=2, now=0)
        j = self.db.claim("w", now=0)
        self.assertEqual(self.db.fail(j["id"], "boom", now=10), "queued")
        self.assertEqual(self.db.get(j["id"])["run_after"], 10 + backoff(1))
        self.assertIsNone(self.db.claim("w", now=11))
        j = self.db.claim("w", now=10 + backoff(1))
        self.assertEqual(self.db.fail(j["id"], "boom", now=100), "failed")
        self.assertEqual(self.db.get(j["id"])["state"], "failed")

    def test_backoff_is_bounded(self):
        self.assertEqual(backoff(1), 30)
        self.assertEqual(backoff(50), J.BACKOFF_CAP)

    def test_crash_recovery_requeues_then_fails(self):
        self.db.enqueue("x", max_attempts=2, timeout_s=10, now=0)
        j = self.db.claim("w", now=0)                       # lease until 0+10+30
        self.assertEqual(self.db.recover(now=39), [])
        (r,) = self.db.recover(now=41)
        self.assertEqual(r["state"], "queued")
        j = self.db.claim("w", now=41 + backoff(1))
        (r,) = self.db.recover(now=10_000)
        self.assertEqual(r["state"], "failed")

    def test_cancel_queued_and_running(self):
        q = self.db.enqueue("x", now=0)
        self.assertEqual(self.db.request_cancel(q), "cancelled")
        r = self.db.enqueue("x", now=0)
        self.db.claim("w", now=0)
        self.assertEqual(self.db.request_cancel(r), "cancelling")
        self.assertTrue(self.db.cancel_requested(r))
        self.assertEqual(self.db.fail(r, "stopped", now=1), "cancelled")      # no retry after a cancel

    def test_defer_does_not_use_an_attempt(self):
        self.db.enqueue("x", max_attempts=1, now=0)
        j = self.db.claim("w", now=0)
        self.db.defer(j["id"], 500, "quota", now=1)
        got = self.db.claim("w", now=500)
        self.assertEqual((got["id"], got["attempts"]), (j["id"], 1))

    def test_exclusive_kinds_run_one_at_a_time(self):
        a = self.db.enqueue("generate_site", now=0)
        b = self.db.enqueue("generate_site", now=0)
        c = self.db.enqueue("collect", now=0)
        first = self.db.claim("w", now=1, exclusive_kinds=("generate_site",))
        second = self.db.claim("w", now=1, exclusive_kinds=("generate_site",))
        self.assertEqual((first["id"], second["id"]), (a, c))              # the second generation job waits
        self.db.complete(a, now=2)
        self.assertEqual(self.db.claim("w", now=3, exclusive_kinds=("generate_site",))["id"], b)

    def test_pause_and_resume(self):
        j = self.db.enqueue("x", now=0)
        self.db.claim("w", now=0)
        self.db.pause(j, "needs setup", now=1)
        self.assertEqual(self.db.get(j)["state"], "paused")
        self.assertIsNone(self.db.claim("w", now=2))
        self.assertTrue(self.db.resume(j, now=3))
        self.assertIsNotNone(self.db.claim("w", now=3))

    def test_checkpoint_survives(self):
        j = self.db.enqueue("x", now=0)
        self.db.claim("w", now=0)
        self.db.checkpoint(j, {"step": 3}, now=1)
        self.assertEqual(self.db.get(j)["checkpoint"], {"step": 3})


class ScheduleTests(Base):
    def test_due_schedule_enqueues_once_per_slot(self):
        self.db.schedule("collect", "collect", 900, now=0, start_at=0)
        self.assertEqual(len(self.db.tick_schedules(now=1)["enqueued"]), 1)
        self.assertEqual(self.db.tick_schedules(now=2)["enqueued"], [])
        self.assertEqual(len(self.db.tick_schedules(now=901)["enqueued"]), 1)

    def test_downtime_fires_once_not_once_per_missed_slot(self):
        self.db.schedule("collect", "collect", 900, now=0, start_at=0)
        res = self.db.tick_schedules(now=900 * 50 + 5)               # 50 slots missed
        self.assertEqual(len(res["enqueued"]), 1)
        self.assertGreater(self.db.schedules()[0]["next_run"], 900 * 50 + 5)

    def test_expired_outreach_slot_is_dropped_not_replayed(self):
        self.db.schedule("outreach", "send_batch", 3600, catchup="skip_expired", max_lateness_s=600, now=0, start_at=0)
        res = self.db.tick_schedules(now=7200)
        self.assertEqual((res["enqueued"], res["skipped"]), ([], ["outreach"]))
        self.assertGreater(self.db.schedules()[0]["next_run"], 7200)
        self.db.schedule("outreach", "send_batch", 3600, catchup="skip_expired", max_lateness_s=600)
        res = self.db.tick_schedules(now=self.db.schedules()[0]["next_run"] + 100)    # only 100s late: still fine
        self.assertEqual(len(res["enqueued"]), 1)

    def test_bad_catchup_policy_rejected(self):
        with self.assertRaises(ValueError):
            self.db.schedule("x", "x", 10, catchup="replay_all")


class OutboxTests(Base):
    def test_idempotency_key_prevents_duplicates(self):
        a, new = self.db.outbox_add("camp:biz1", {"to": "x"}, campaign_id="c")
        b, again = self.db.outbox_add("camp:biz1", {"to": "x"}, campaign_id="c")
        self.assertEqual((a, new, b, again), (a, True, a, False))

    def test_claim_marks_sending_before_the_provider_is_called(self):
        self.db.outbox_add("k", {"to": "x"})
        row = self.db.outbox_claim()
        self.assertEqual(self.db.outbox_list(["sending"])[0]["id"], row["id"])
        self.assertIsNone(self.db.outbox_claim())                   # nobody else can take it
        self.db.outbox_sent(row["id"], "msg1", "thr1")
        got = self.db.outbox_list(["sent"])[0]
        self.assertEqual((got["provider_message_id"], got["provider_thread_id"]), ("msg1", "thr1"))

    def test_crash_during_send_becomes_uncertain_never_requeued(self):
        self.db.outbox_add("k", {"to": "x"})
        self.db.outbox_claim()
        self.assertEqual(self.db.outbox_recover(), 1)
        self.assertEqual(self.db.outbox_list(["uncertain"])[0]["idem_key"], "k")
        self.assertIsNone(self.db.outbox_claim())

    def test_reconcile_found_notfound_and_unknown(self):
        for k in "abc":
            self.db.outbox_add(k, {"to": k})
            self.db.outbox_claim()
        self.db.outbox_recover()
        ids = {r["idem_key"]: r["id"] for r in self.db.outbox_list()}
        self.assertEqual(self.db.reconcile(ids["a"], lambda r: {"found": True, "message_id": "m", "thread_id": "t"}), "sent")
        self.assertEqual(self.db.reconcile(ids["b"], lambda r: {"found": False, "authoritative": True}), "queued")
        self.assertEqual(self.db.reconcile(ids["c"], lambda r: {"found": False}), "held")       # can't prove: held
        self.db.outbox_set(ids["c"], "uncertain")
        def boom(r): raise TimeoutError()
        self.assertEqual(self.db.reconcile(ids["c"], boom), "held")

    def test_daily_cap_counts_possible_sends(self):
        for k in "abc":
            self.db.outbox_add(k, {})
        r = self.db.outbox_claim(); self.db.outbox_sent(r["id"], "m")
        self.db.outbox_claim()                                          # stays 'sending'
        self.assertEqual(self.db.outbox_count_sent_since(0), 2)


class LockTests(Base):
    def test_one_executor_per_campaign(self):
        self.assertTrue(self.db.acquire_lock("camp", "A", 60, now=0))
        self.assertFalse(self.db.acquire_lock("camp", "B", 60, now=10))
        self.assertTrue(self.db.acquire_lock("camp", "A", 60, now=10))   # renew
        self.assertTrue(self.db.acquire_lock("camp", "B", 60, now=100))  # A's lease ran out
        self.db.release_lock("camp", "A")                                # not A's any more: no effect
        self.assertFalse(self.db.acquire_lock("camp", "A", 60, now=110))


class RuntimeTests(Base):
    def make(self, handlers, clock=None):
        w = Worker(self.dir, handlers=handlers, tick=0.01, **({"clock": clock} if clock else {}))
        self.addCleanup(w.keep_awake.stop)
        return w

    def test_runs_job_and_heartbeat(self):
        seen = []
        w = self.make({"x": lambda ctx, job: seen.append(job["payload"]) or {"ok": 1}})
        jid = w.db.enqueue("x", {"a": 1})
        w.run_once()
        self.assertEqual(seen, [{"a": 1}])
        self.assertEqual(w.db.get(jid)["state"], "done")
        self.assertIsInstance(w.events.get("worker_heartbeat"), float)
        self.assertEqual(w.events.sync_state("worker")["state"], "ok")

    def test_kill_switch_blocks_new_jobs_and_external_actions(self):
        ran = []
        w = self.make({"x": lambda ctx, job: ran.append(ctx.external_allowed())})
        w.db.enqueue("x")
        w.kill_path.parent.mkdir(parents=True, exist_ok=True)
        w.kill_path.write_text("1")
        w.run_once()
        self.assertEqual(ran, [])
        self.assertEqual(w.db.counts(), {"queued": 1})
        w.kill_path.unlink()
        w.run_once()
        self.assertEqual(ran, [True])

    def test_kill_switch_flipped_mid_job_is_seen(self):
        def handler(ctx, job):
            ctx.worker.kill_path.write_text("1")
            return {"stopped": ctx.should_stop(), "external": ctx.external_allowed()}
        w = self.make({"x": handler})
        jid = w.db.enqueue("x")
        w.run_once()
        self.assertEqual(w.db.get(jid)["result"], {"stopped": True, "external": False})

    def test_failure_retries_then_records_event(self):
        w = self.make({"x": lambda c, j: (_ for _ in ()).throw(RuntimeError("nope"))})
        jid = w.db.enqueue("x", max_attempts=1)
        w.run_once()
        self.assertEqual(w.db.get(jid)["state"], "failed")
        (e,) = w.events.events(kinds=("job_failed",))
        self.assertEqual(e["status"], "open")

    def test_needs_setup_pauses_and_reports(self):
        w = self.make({"x": lambda c, j: (_ for _ in ()).throw(NeedsSetup("Gmail spam account isn't connected"))})
        jid = w.db.enqueue("x")
        w.run_once()
        self.assertEqual(w.db.get(jid)["state"], "paused")
        (e,) = w.events.events(kinds=("connection_missing",))
        self.assertIn("Gmail", e["title"])

    def test_fatal_does_not_retry(self):
        w = self.make({"x": lambda c, j: (_ for _ in ()).throw(Fatal("policy"))})
        jid = w.db.enqueue("x", max_attempts=5)
        w.run_once()
        self.assertEqual(w.db.get(jid)["state"], "failed")

    def test_deferred_requeues_without_losing_an_attempt(self):
        w = self.make({"x": lambda c, j: (_ for _ in ()).throw(Deferred(time.time() + 3600, "daily cap reached"))})
        jid = w.db.enqueue("x", max_attempts=1)
        w.run_once()
        job = w.db.get(jid)
        self.assertEqual((job["state"], job["attempts"]), ("queued", 0))
        self.assertGreater(job["run_after"], time.time() + 3000)

    def test_timeout_abandons_job_and_ignores_late_result(self):
        release = threading.Event()
        def slow(ctx, job):
            release.wait(5)
            return {"late": True}
        w = self.make({"x": slow})
        jid = w.db.enqueue("x", timeout_s=0.1, max_attempts=1)
        w.run_once()
        release.set()
        time.sleep(0.1)
        self.assertEqual(w.db.get(jid)["state"], "failed")
        self.assertEqual(w.db.get(jid)["result"], {})

    def test_restart_recovers_interrupted_job_and_send(self):
        w = self.make({"x": lambda c, j: {"ok": 1}})
        jid = w.db.enqueue("x", timeout_s=1, now=0)
        w.db.claim("dead-worker", now=0)                                # a worker died holding it
        w.db.outbox_add("k", {})
        w.db.outbox_claim()
        w.run_once()                                                    # new worker starts: real clock, lease long expired
        self.assertEqual(w.db.get(jid)["state"], "queued")              # recovered, retried after a backoff
        self.assertGreater(w.db.get(jid)["attempts"], 0)
        self.assertEqual(w.db.outbox_list(["uncertain"])[0]["idem_key"], "k")

    def test_sleep_gap_does_not_replay_expired_outreach(self):
        now = [1000.0]
        w = self.make({}, clock=lambda: now[0])
        w.db.schedule("outreach", "send_batch", 3600, catchup="skip_expired", max_lateness_s=600, now=1000, start_at=1000)
        now[0] = 1000 + 8 * 3600                                         # Mac slept 8 hours
        w.run_once()
        self.assertEqual(w.db.list(), [])
        self.assertTrue(w.events.events(kinds=("worker_started",)))     # the resume was recorded

    def test_selftest_and_collect_handlers(self):
        w = self.make(default_handlers())
        a = w.db.enqueue("selftest", {"echo": "hi"})
        w.run_once()
        self.assertEqual(w.db.get(a)["result"], {"ok": True, "echo": "hi"})
        self.assertEqual(w.db.get(a)["checkpoint"], {"step": 1})

    def test_only_one_worker_process_per_mac(self):
        w1, w2 = self.make({}), self.make({})
        self.assertTrue(w1.acquire_process_lock())
        self.assertFalse(w2.acquire_process_lock())


class PowerTests(unittest.TestCase):
    def test_parse_power(self):
        ac = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t83%; charging; 1:00 remaining"
        bat = "Now drawing from 'Battery Power'\n -InternalBattery-0\t35%; discharging"
        self.assertEqual(parse_power(ac), {"ac": True, "percent": 83})
        self.assertEqual(parse_power(bat), {"ac": False, "percent": 35})
        self.assertEqual(parse_power(""), {"ac": None, "percent": None})

    def test_keep_awake_limits(self):
        self.assertTrue(keep_awake_allowed({"ac": True, "percent": 10}, 40))
        self.assertTrue(keep_awake_allowed({"ac": False, "percent": 60}, 40))
        self.assertFalse(keep_awake_allowed({"ac": False, "percent": 30}, 40))
        self.assertFalse(keep_awake_allowed({"ac": None, "percent": None}, 40))

    def test_keep_awake_is_opt_in_and_released(self):
        procs = []
        class P:
            def __init__(self, cmd): procs.append(cmd); self.dead = False
            def poll(self): return 1 if self.dead else None
            def terminate(self): self.dead = True
        ka = KeepAwake(popen=P)
        self.assertFalse(ka.update(False, {"ac": True, "percent": 100}, 40))
        self.assertEqual(procs, [])                                      # off by default
        self.assertTrue(ka.update(True, {"ac": True, "percent": 100}, 40, pid=42))
        self.assertEqual(procs[0], ["/usr/bin/caffeinate", "-i", "-w", "42"])   # never touches pmset settings
        self.assertFalse(ka.update(True, {"ac": False, "percent": 10}, 40))     # unplugged and low: let go


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.calls = []
        self.rc = 0

    def tearDown(self):
        self.tmp.cleanup()

    def lc(self, args):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.rc, "", "denied" if self.rc else "")

    def test_plist_uses_absolute_paths_and_restarts_only_on_crash(self):
        p = launchd.build_plist(python="/usr/bin/python3", repo=Path("/r"), log=Path("/r/w.log"))
        self.assertEqual(p["ProgramArguments"], ["/usr/bin/python3", "-u", "-m", "worker", "run"])
        self.assertEqual((p["WorkingDirectory"], p["RunAtLoad"]), ("/r", True))
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})

    def test_install_writes_loads_and_explains_limits(self):
        lines = launchd.install(python="/py", repo=Path("/r"), log=Path("/r/w.log"), home=self.home, uid=501, launchctl=self.lc)
        data = plistlib.loads(launchd.plist_path(self.home).read_bytes())
        self.assertEqual(data["Label"], launchd.LABEL)
        self.assertIn(["bootstrap", "gui/501", str(launchd.plist_path(self.home))], self.calls)
        self.assertTrue(any("sleeps" in l for l in lines))
        self.assertFalse(launchd.backup_path(self.home).exists())

    def test_reinstall_keeps_backup_and_rollback_restores_it(self):
        launchd.install(python="/old", repo=Path("/r"), log=Path("/l"), home=self.home, uid=1, launchctl=self.lc)
        launchd.install(python="/new", repo=Path("/r"), log=Path("/l"), home=self.home, uid=1, launchctl=self.lc)
        self.assertEqual(plistlib.loads(launchd.plist_path(self.home).read_bytes())["ProgramArguments"][0], "/new")
        launchd.rollback(home=self.home, uid=1, launchctl=self.lc)
        self.assertEqual(plistlib.loads(launchd.plist_path(self.home).read_bytes())["ProgramArguments"][0], "/old")

    def test_rollback_without_backup_removes_the_job(self):
        launchd.install(python="/p", repo=Path("/r"), log=Path("/l"), home=self.home, uid=1, launchctl=self.lc)
        out = launchd.rollback(home=self.home, uid=1, launchctl=self.lc)
        self.assertFalse(launchd.plist_path(self.home).exists())
        self.assertTrue(any("no earlier version" in l for l in out))

    def test_uninstall_and_restart_and_failures(self):
        launchd.install(python="/p", repo=Path("/r"), log=Path("/l"), home=self.home, uid=1, launchctl=self.lc)
        launchd.uninstall(home=self.home, uid=1, launchctl=self.lc)
        self.assertFalse(launchd.plist_path(self.home).exists())
        launchd.restart(uid=1, launchctl=self.lc)
        self.assertEqual(self.calls[-1], ["kickstart", "-k", f"gui/1/{launchd.LABEL}"])
        self.rc = 1
        with self.assertRaises(RuntimeError):
            launchd.restart(uid=1, launchctl=self.lc)
        with self.assertRaises(RuntimeError):
            launchd.install(python="/p", repo=Path("/r"), log=Path("/l"), home=self.home, uid=1, launchctl=self.lc)


class HeadlessTests(unittest.TestCase):
    def test_worker_never_imports_audio_ui_or_wake_code(self):
        code = (
            f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
            "import worker, worker.jobs, worker.runtime, worker.launchd\n"
            "from worker.runtime import default_handlers\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('PyQt6','PyQt5','PySide6','sounddevice','pyaudio',"
            "'openwakeword','ui','main','cv2') or m in ('core.wake_word','core.audio_devices','core.hotkey','core.live_session')]\n"
            "print('BAD', bad)\n")
        out = subprocess.run([sys.executable, "-I", "-c", code], cwd=REPO, capture_output=True, text=True,
                             env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}).stdout
        self.assertIn("BAD []", out)

    def test_collect_handler_stays_headless(self):
        code = (
            f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
            "import tempfile\nfrom pathlib import Path\nfrom core.events import Store\nfrom core import away\n"
            "away.ingest_trading(Store(Path(tempfile.mkdtemp())/'e.db'), Path(tempfile.mkdtemp()))\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('PyQt6','sounddevice','pyaudio','openwakeword','ui','main')]\n"
            "print('BAD', bad)\n")
        out = subprocess.run([sys.executable, "-I", "-c", code], cwd=REPO, capture_output=True, text=True,
                             env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}).stdout
        self.assertIn("BAD []", out)


if __name__ == "__main__":
    unittest.main()
