"""The worker loop: heartbeat, crash recovery, schedules, one job at a time.

Rules it keeps:
  * the KILL file is checked before every new job and before every external action;
  * one job at a time (generation jobs are additionally marked exclusive);
  * a job that exceeds its timeout is abandoned (its late result is ignored) and retried
    with backoff; a job that was running when the worker died is recovered from its lease;
  * a crash or Mac sleep never replays old outreach: schedules fire at most once after
    downtime and expired time-sensitive slots are dropped (worker/jobs.py);
  * it never changes the Mac's power settings. Keep-awake is an explicit opt-in that holds a
    `caffeinate -i` while on AC power (or above a battery floor) and lets go otherwise.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core.events import Store
from worker.jobs import JobDB

log = logging.getLogger("jarvis.worker")

TICK_SECONDS = 5.0
HEARTBEAT_STALE_AFTER = 120.0
EXCLUSIVE_KINDS = ("generate_site", "build_site")      # one generation job at a time


class Fatal(Exception):
    """Do not retry this job (bad input, policy refusal)."""


class NeedsSetup(Exception):
    """A connection or setting is missing. The job is paused and you are told, not retried."""


class Deferred(Exception):
    """Not now (quota reached, outside the allowed hours). Requeued without using an attempt."""

    def __init__(self, until: float, reason: str):
        super().__init__(reason)
        self.until, self.reason = until, reason


def default_dir() -> Path:
    from memory.config_manager import CONFIG_DIR
    return CONFIG_DIR / "worker"


# ── Mac state ────────────────────────────────────────────────────────────────
def parse_power(text: str) -> dict:
    """{"ac": bool|None, "percent": int|None} from `pmset -g batt` output."""
    ac = None
    if "'AC Power'" in text:
        ac = True
    elif "'Battery Power'" in text:
        ac = False
    m = re.search(r"(\d{1,3})%", text)
    return {"ac": ac, "percent": int(m.group(1)) if m else None}


def read_power() -> dict:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, timeout=4).stdout.decode("utf-8", "replace")
        return parse_power(out)
    except (OSError, subprocess.SubprocessError):
        return {"ac": None, "percent": None}


def keep_awake_allowed(power: dict, min_battery: int) -> bool:
    if power.get("ac"):
        return True
    pct = power.get("percent")
    return power.get("ac") is False and isinstance(pct, int) and pct >= min_battery


class KeepAwake:
    """Optional. Holds the Mac awake (not the display) only while enabled AND power allows."""

    def __init__(self, popen=subprocess.Popen):
        self._popen, self._proc = popen, None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def update(self, enabled: bool, power: dict, min_battery: int, pid: Optional[int] = None) -> bool:
        want = enabled and keep_awake_allowed(power, min_battery)
        if want and not self.active:
            try:
                self._proc = self._popen(["/usr/bin/caffeinate", "-i", "-w", str(pid or os.getpid())])
            except OSError:
                self._proc = None
        elif not want and self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None
        return self.active

    def stop(self) -> None:
        self.update(False, {}, 0)


# ── job context ──────────────────────────────────────────────────────────────
class JobContext:
    def __init__(self, worker: "Worker", job: dict, deadline: float):
        self.worker, self.job, self.deadline = worker, job, deadline
        self.db, self.events = worker.db, worker.events
        self.abandoned = False

    def should_stop(self) -> bool:
        """True when the job should wind down: kill switch, cancel request, timeout or shutdown."""
        return (self.abandoned or self.worker.killed() or self.worker.stop_event.is_set()
                or time.time() > self.deadline or self.db.cancel_requested(self.job["id"]))

    def checkpoint(self, data: dict) -> None:
        self.db.checkpoint(self.job["id"], data)

    def external_allowed(self) -> bool:
        """Call before any action that leaves this Mac (send, publish). False if killed."""
        return not self.worker.killed()


class Worker:
    def __init__(self, directory: Optional[Path] = None, *, db: Optional[JobDB] = None,
                 events: Optional[Store] = None, handlers: Optional[dict] = None,
                 owner: Optional[str] = None, tick: float = TICK_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(directory) if directory else default_dir()
        self.db = db or JobDB(self.dir / "jobs.db")
        self.events = events or Store(self.dir.parent / "events.db")
        self.handlers = dict(handlers or {})
        self.owner = owner or f"worker-{os.uname().nodename if hasattr(os, 'uname') else 'host'}-{os.getpid()}"
        self.tick, self.clock = tick, clock
        self.stop_event = threading.Event()
        self.keep_awake = KeepAwake()
        self._last_tick = clock()
        self._started = clock()
        self._lock_fd = None
        self.kill_path = self.dir / "KILL"
        self.status_path = self.dir / "status.json"

    # ── controls ─────────────────────────────────────────────────────────────
    def killed(self) -> bool:
        return self.kill_path.exists()

    def acquire_process_lock(self) -> bool:
        """Only one worker per Mac (and so one executor per local campaign)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = open(self.dir / "worker.lock", "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return False
        fd.write(str(os.getpid()))
        fd.flush()
        self._lock_fd = fd
        return True

    # ── one pass ─────────────────────────────────────────────────────────────
    def heartbeat(self, mac: Optional[dict] = None) -> None:
        now = self.clock()
        self.events.set("worker_heartbeat", now)
        self.events.sync_ok("worker", stale_after=HEARTBEAT_STALE_AFTER, at=now)
        status = {"pid": os.getpid(), "owner": self.owner, "started": self._started, "heartbeat": now,
                  "killed": self.killed(), "jobs": self.db.counts(), "mac": mac or {},
                  "keep_awake": self.keep_awake.active}
        tmp = self.status_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(status))
            os.replace(tmp, self.status_path)
        except OSError:
            pass

    def _mac_state(self) -> dict:
        from core import away
        return {"screen": away.screen_state(), "power": read_power()}

    def _settings(self) -> tuple[bool, int]:
        try:
            from memory.config_manager import load_api_keys
            cfg = load_api_keys()
            return bool(cfg.get("worker_keep_awake", False)), int(cfg.get("worker_keep_awake_min_battery", 40))
        except Exception:
            return False, 40

    def run_once(self) -> Optional[dict]:
        """One iteration. Returns the job that ran (or None)."""
        now = self.clock()
        gap = now - self._last_tick
        self._last_tick = now
        if gap > max(60.0, self.tick * 6):
            # The Mac slept (or the process was suspended). Nothing is replayed: see tick_schedules.
            log.info("resumed after %.0f s gap (sleep or suspend)", gap)
            self.events.record("worker_started", source="worker", source_id=f"resume:{int(now)}", ts=now,
                               title="Worker resumed after sleep", detail={"gap_s": int(gap)})
        mac = self._mac_state()
        enabled, floor = self._settings()
        self.keep_awake.update(enabled, mac["power"], floor)
        self.heartbeat(mac)
        for r in self.db.recover(now):
            log.warning("recovered job %s (%s) -> %s", r["id"], r["kind"], r["state"])
            if r["state"] == "failed":
                self._failed_event(r["id"], r["kind"], "worker stopped or timed out; attempts used up", r["campaign_id"], now)
        stuck = self.db.outbox_recover(now)
        if stuck:
            log.warning("%d send(s) were interrupted and are now 'uncertain' (never auto-resent)", stuck)
        if self.killed():
            return None
        self.db.tick_schedules(now)
        job = self.db.claim(self.owner, now=now, exclusive_kinds=EXCLUSIVE_KINDS, only_kinds=tuple(self.handlers))
        if job is None:
            return None
        self._execute(job)
        return job

    def _failed_event(self, job_id: int, kind: str, error: str, campaign: str, now: float) -> None:
        self.events.record("job_failed", source="worker", source_id=f"job:{job_id}", ts=now, task_id=str(job_id),
                           campaign_id=campaign, status="open", title=f"Background job {kind} failed",
                           detail={"error": error[:200], "job_id": job_id}, evidence={"job_id": job_id})

    def _execute(self, job: dict) -> None:
        handler = self.handlers.get(job["kind"])
        ctx = JobContext(self, job, self.clock() + float(job["timeout_s"]))
        box: dict = {}

        def target():
            try:
                box["result"] = handler(ctx, job)
            except BaseException as exc:                     # reported below, never lost
                box["error"] = exc

        t = threading.Thread(target=target, daemon=True, name=f"job-{job['id']}")
        t.start()
        t.join(float(job["timeout_s"]))
        now = self.clock()
        if t.is_alive():
            ctx.abandoned = True                              # its late result is ignored
            state = self.db.fail(job["id"], f"timed out after {job['timeout_s']:g}s", now=now)
            log.error("job %s timed out", job["id"])
            if state == "failed":
                self._failed_event(job["id"], job["kind"], "timed out", job["campaign_id"], now)
            return
        err = box.get("error")
        if err is None:
            if self.db.cancel_requested(job["id"]) and not box.get("result"):
                self.db.mark_cancelled(job["id"], now)
            else:
                self.db.complete(job["id"], box.get("result") if isinstance(box.get("result"), dict) else {}, now)
            return
        if isinstance(err, Deferred):
            self.db.defer(job["id"], err.until, err.reason, now)
        elif isinstance(err, NeedsSetup):
            self.db.pause(job["id"], str(err), now)
            self.events.record("connection_missing", source="worker", source_id=f"job:{job['id']}:setup", ts=now,
                               task_id=str(job["id"]), campaign_id=job["campaign_id"], status="open",
                               title=str(err)[:200], detail={"job_id": job["id"], "kind": job["kind"]})
        elif isinstance(err, Fatal):
            self.db.fail(job["id"], str(err), retry=False, now=now)
            self._failed_event(job["id"], job["kind"], str(err), job["campaign_id"], now)
        elif isinstance(err, (KeyboardInterrupt, SystemExit)):
            raise err
        else:
            log.exception("job %s failed", job["id"], exc_info=err)
            state = self.db.fail(job["id"], f"{type(err).__name__}: {err}", now=now)
            if state == "failed":
                self._failed_event(job["id"], job["kind"], f"{type(err).__name__}: {err}", job["campaign_id"], now)

    # ── forever ──────────────────────────────────────────────────────────────
    def run_forever(self) -> None:
        self.events.record("worker_started", source="worker", source_id=f"start:{int(self._started)}",
                           ts=self._started, title="Worker started", detail={"pid": os.getpid()})
        self.stop_event.clear()
        while not self.stop_event.is_set():
            try:
                ran = self.run_once()
            except Exception:
                log.exception("worker pass failed")
                ran = None
            if ran is None:
                self.stop_event.wait(self.tick)
        self.shutdown()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())

    def shutdown(self) -> None:
        self.keep_awake.stop()
        self.events.record("worker_stopped", source="worker", source_id=f"stop:{int(self.clock())}",
                           ts=self.clock(), title="Worker stopped")
        try:
            self.status_path.unlink()
        except OSError:
            pass


# ── built-in handlers ────────────────────────────────────────────────────────
def handle_collect(ctx: JobContext, job: dict) -> dict:
    """Refresh the briefing's local sources (paper-trader journal, promotion history)."""
    from core import away
    away.collect(ctx.events)
    return {"refreshed": True}


def handle_selftest(ctx: JobContext, job: dict) -> dict:
    """A harmless job for checking the worker end to end."""
    ctx.checkpoint({"step": 1})
    return {"ok": True, "echo": job["payload"].get("echo")}


def default_handlers() -> dict:
    return {"collect": handle_collect, "selftest": handle_selftest}


def seed_default_schedules(db: JobDB, now: Optional[float] = None) -> None:
    db.schedule("collect", "collect", 15 * 60, catchup="run_once", now=now)
