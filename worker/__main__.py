"""Command line for the Jarvis background worker.

    python3 -m worker run            run in the foreground (this is what launchd runs)
    python3 -m worker status         is it running, last heartbeat, jobs, Mac state
    python3 -m worker stop           ask a running worker to finish its job and exit
    python3 -m worker kill on|off    the kill switch: no new jobs, no sends, until `kill off`
    python3 -m worker jobs [state]   list jobs (queued, running, done, failed, paused, cancelled)
    python3 -m worker cancel ID      cancel a job
    python3 -m worker resume ID      resume a paused job
    python3 -m worker selftest       queue a harmless test job
    python3 -m worker logs [N]       the last N log lines
    python3 -m worker install | uninstall | restart | rollback    the per-user launchd job (macOS)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from worker import jobs as jobs_mod                   # noqa: E402
from worker.runtime import Worker, default_dir, default_handlers, seed_default_schedules   # noqa: E402


def _setup_logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("jarvis")
    root.setLevel(logging.INFO)
    file_h = logging.handlers.RotatingFileHandler(directory / "worker.log", maxBytes=1_000_000, backupCount=3)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)
    if sys.stderr.isatty():
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def _handlers() -> dict:
    handlers = default_handlers()
    campaign = _campaign()
    if campaign:
        handlers.update(campaign.handlers())
    return handlers


def _campaign():
    """The website campaign module, if this checkout has it. A real error inside it is not hidden."""
    import importlib
    try:
        return importlib.import_module("worker.campaign")
    except ModuleNotFoundError as exc:
        if exc.name == "worker.campaign":
            return None
        raise


def cmd_run(args) -> int:
    d = default_dir()
    _setup_logging(d)
    w = Worker(d, handlers=_handlers())
    if not w.acquire_process_lock():
        print("Another worker is already running on this Mac (only one runs at a time).")
        return 0                                        # exit 0: launchd must not respawn a duplicate in a loop
    seed_default_schedules(w.db)
    campaign = _campaign()
    if campaign:
        campaign.seed_schedules(w.db)
    w.install_signal_handlers()
    logging.getLogger("jarvis.worker").info("worker started pid=%s", os.getpid())
    w.run_forever()
    return 0


def _read_status(d: Path):
    try:
        return json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        return None


def _lock_holder(d: Path):
    """pid of the live worker, or None. Liveness is the process lock itself (the OS drops it when the
    process dies), so a stale pid file or a reused pid can never be mistaken for the worker."""
    import fcntl
    try:
        fd = open(d / "worker.lock", "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.seek(0)
            try:
                return int(fd.read().strip())
            except ValueError:
                return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        fd.close()


def cmd_status(args) -> int:
    from core.events import Store
    d = default_dir()
    s = _read_status(d)
    now = time.time()
    if s and _lock_holder(d) is not None:
        age = now - float(s["heartbeat"])
        print(f"Worker: RUNNING (pid {s['pid']}), last heartbeat {age:.0f}s ago" + ("  [STALE]" if age > 120 else ""))
        print(f"Mac: screen {s.get('mac', {}).get('screen', '?')}, power {s.get('mac', {}).get('power', {})}, "
              f"keep-awake {'on' if s.get('keep_awake') else 'off'}")
    else:
        print("Worker: NOT RUNNING (nothing is being done in the background)")
    print("Kill switch: " + ("ON, no new jobs or sends" if (d / "KILL").exists() else "off"))
    try:
        print("Jobs:", jobs_mod.JobDB(d / "jobs.db").counts() or "none yet")
        st = Store(d.parent / "events.db").sync_state("worker")
        print("Last good sync:", st["state"])
    except Exception as exc:
        print(f"Jobs: unavailable ({exc!r})")
    if sys.platform == "darwin":
        from worker import launchd
        present, loaded = launchd.status()
        print(f"Starts at login: {'yes' if present and loaded else 'file present but not loaded' if present else 'no (run `install`)'}")
    return 0


def cmd_stop(args) -> int:
    pid = _lock_holder(default_dir())
    if pid is None:
        print("The worker isn't running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("Asked the worker to finish its current job and exit.")
    return 0


def cmd_kill(args) -> int:
    d = default_dir()
    d.mkdir(parents=True, exist_ok=True)
    flag = d / "KILL"
    if args.mode == "on":
        flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        print("Kill switch ON: no new jobs and no external actions until you run `kill off`.")
    elif args.mode == "off":
        flag.unlink(missing_ok=True)
        print("Kill switch off.")
    else:
        print("on" if flag.exists() else "off")
    return 0


def cmd_jobs(args) -> int:
    db = jobs_mod.JobDB(default_dir() / "jobs.db")
    rows = db.list([args.state] if args.state else None, limit=args.limit)
    if not rows:
        print("No jobs.")
    for r in rows:
        err = f"  ({r['last_error'][:60]})" if r["last_error"] else ""
        print(f"#{r['id']:<4} {r['state']:<9} {r['kind']:<18} attempts {r['attempts']}/{r['max_attempts']}{err}")
    return 0


def cmd_cancel(args) -> int:
    print(jobs_mod.JobDB(default_dir() / "jobs.db").request_cancel(args.id))
    return 0


def cmd_resume(args) -> int:
    print("Resumed." if jobs_mod.JobDB(default_dir() / "jobs.db").resume(args.id) else "That job isn't paused.")
    return 0


def cmd_selftest(args) -> int:
    jid = jobs_mod.JobDB(default_dir() / "jobs.db").enqueue("selftest", {"echo": "hello"}, unique_key=f"selftest:{int(time.time())}")
    print(f"Queued test job #{jid}. With the worker running, `python3 -m worker jobs` should show it done.")
    return 0


def cmd_logs(args) -> int:
    try:
        lines = (default_dir() / "worker.log").read_text(errors="replace").splitlines()[-args.n:]
    except OSError:
        print("No log yet.")
        return 0
    print("\n".join(lines))
    return 0


def _launchd(action):
    def run(args) -> int:
        from worker import launchd
        d = default_dir()
        try:
            if action == "install":
                lines = launchd.install(repo=REPO, log=d / "launchd.log")
            elif action == "uninstall":
                lines = launchd.uninstall()
            elif action == "restart":
                lines = launchd.restart()
            else:
                lines = launchd.rollback()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(exc)
            return 1
        print("\n".join(lines))
        return 0
    return run


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="worker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    k = sub.add_parser("kill"); k.add_argument("mode", choices=["on", "off", "status"], nargs="?", default="status"); k.set_defaults(fn=cmd_kill)
    j = sub.add_parser("jobs"); j.add_argument("state", nargs="?"); j.add_argument("--limit", type=int, default=30); j.set_defaults(fn=cmd_jobs)
    c = sub.add_parser("cancel"); c.add_argument("id", type=int); c.set_defaults(fn=cmd_cancel)
    r = sub.add_parser("resume"); r.add_argument("id", type=int); r.set_defaults(fn=cmd_resume)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    lg = sub.add_parser("logs"); lg.add_argument("n", type=int, nargs="?", default=40); lg.set_defaults(fn=cmd_logs)
    for name in ("install", "uninstall", "restart", "rollback"):
        sub.add_parser(name).set_defaults(fn=_launchd(name))
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
