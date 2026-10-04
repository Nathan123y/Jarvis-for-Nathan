"""
core/perf_trace.py — Lightweight runtime diagnostics for JARVIS.

WHY THIS EXISTS
---------------
"It lags from the Finder app but not from VS Code" and "the HUD flickers
between states while JARVIS is still talking" are both timing problems, not
content problems — so this module records *only* numbers, timestamps and
short enum-like labels. It never writes transcript text, memory entries,
note contents, Gmail/iMessage content, or anything that came from a
conversation. It is safe to leave enabled permanently and safe to attach to
a bug report as-is.

Everything here is best-effort and defensive, matching the rest of the
codebase: a tracing failure must never be able to affect playback or the
session. Records are written by a bounded background worker so audio threads
never perform disk I/O. A full queue drops diagnostic records.

OUTPUT
------
One JSON object per line, appended to:
    ~/Library/Logs/Jarvis/perf.jsonl      (macOS)
    <project>/logs/perf.jsonl             (fallback, any OS)

Nothing reads this file back at runtime — delete or rotate it any time.

INTEGRATION
-----------
main.py records launch context on a background thread, every speaker write
(buffer size, latency, underflow, how many chunks were waiting, how long the
write blocked and how long since the previous one), input overflow reports,
requested UI state transitions with the reason for each, session reconnects
with a coarse label, and "lag" records whenever the asyncio loop or the Qt
thread was scheduled much later than it asked to be. The state log shows what
Jarvis asked the UI to display; it does not measure when Qt actually painted
a frame. Other call sites may use trace_block() to record suspected
synchronous stalls when needed.

Every record carries the process id, so a VS Code run and a Finder run can be
told apart even in one file. Run `python3 tools/analyze_perf.py` to turn the
log into a short per-run summary. Launch paths and Git status are diagnostics;
review them before sharing the log.

The file is rotated to perf.jsonl.1 when it passes about 20 MB.
"""
from __future__ import annotations

import json
import queue
import os
import platform
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_LOCK = threading.Lock()
_STARTED = time.monotonic()
_SEQ = 0
_RECORDS: queue.Queue[dict] = queue.Queue(maxsize=2048)
_WRITER_STARTED = False
_MAX_LOG_BYTES = 20 * 1024 * 1024


def _log_path() -> Path:
    try:
        if platform.system() == "Darwin":
            base = Path.home() / "Library" / "Logs" / "Jarvis"
        else:
            base = Path(__file__).resolve().parent.parent / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / "perf.jsonl"
    except Exception:
        # Last-resort fallback so a missing/unwritable log dir can never
        # raise out of a tracing call.
        return Path.home() / "jarvis_perf_fallback.jsonl"


def _rotate_if_large(path: Path, limit: int = _MAX_LOG_BYTES) -> bool:
    """Move an oversized log aside once, at startup. Returns True if rotated.

    Only the previous generation is kept, so the log can never grow without
    bound however long the app has been in use.
    """
    try:
        if path.exists() and path.stat().st_size > limit:
            os.replace(path, path.with_name(path.name + ".1"))
            return True
    except Exception:
        pass
    return False


def _write_record_loop() -> None:
    """Keep file I/O off CoreAudio and the asyncio playback loop."""
    path = _log_path()
    _rotate_if_large(path)
    while True:
        record = _RECORDS.get()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:
            pass
        finally:
            _RECORDS.task_done()


def _write(record: dict) -> None:
    global _SEQ, _WRITER_STARTED
    try:
        with _LOCK:
            if not _WRITER_STARTED:
                threading.Thread(
                    target=_write_record_loop, name="jarvis-perf-writer", daemon=True
                ).start()
                _WRITER_STARTED = True
            _SEQ += 1
            record["seq"] = _SEQ
            record["pid"] = os.getpid()
            record["t"] = round(time.monotonic() - _STARTED, 4)
            _RECORDS.put_nowait(record)
    except Exception:
        pass  # a full queue or tracing error must never delay the audio path


def log_launch_context(base_dir: Path) -> None:
    """Call once at startup. Captures exactly the facts that tell a
    Finder-launched bundle apart from a VS Code / Terminal launch —
    interpreter path, CPU architecture (catches Rosetta translation),
    working directory, and which git commit/branch is actually running.
    Never file contents, never config values.
    """
    try:
        info = {
            "kind": "launch",
            "executable": sys.executable,
            "machine": platform.machine(),       # "arm64" vs "x86_64"
            "python": platform.python_version(),
            "cwd": os.getcwd(),
            "base_dir": str(base_dir),
            "frozen": bool(getattr(sys, "frozen", False)),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            # Set only by the Finder-launched app. Tells the two launch paths
            # apart without having to infer it from the parent process.
            "launched_by_app": bool(os.environ.get("JARVIS_MENU_CONTROL_DIR")),
            "tty": _isatty(),
            "cpu_count": os.cpu_count(),
        }
        try:
            info["nice"] = os.nice(0)            # scheduling priority adjustment
        except Exception:
            pass
        try:
            head = subprocess.run(
                ["git", "-C", str(base_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            if head.returncode == 0:
                info["git_commit"] = head.stdout.strip()
            branch = subprocess.run(
                ["git", "-C", str(base_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            if branch.returncode == 0:
                info["git_branch"] = branch.stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(base_dir), "status", "--porcelain"],
                capture_output=True, text=True, timeout=2,
            )
            if dirty.returncode == 0:
                info["git_dirty"] = bool(dirty.stdout.strip())
        except Exception:
            pass  # git not on PATH inside the bundle, or not a git checkout
        _write(info)
    except Exception:
        pass


def log_state_transition(from_state: str, to_state: str, reason: str) -> None:
    """Labels only — never the text that triggered the change."""
    _write({"kind": "state", "from": from_state, "to": to_state, "reason": reason})


def log_audio_write(stream_latency: float, frames: int, underflow: bool,
                    queued_chunks: int | None = None,
                    write_ms: float | None = None,
                    gap_ms: float | None = None) -> None:
    """Call right after every write to the output device. Uses the device's
    own reported latency so a 500 ms buffer isn't penalised against a 50 ms
    one, and records whether PortAudio signalled an underflow.

    The optional fields are what tell the two kinds of underflow apart:
      queued_chunks  50 ms chunks still waiting in the playback queue when the
                     write began. Zero at an underflow means the network or
                     model had not delivered more audio yet; more than zero
                     means audio was waiting and this process was slow to
                     hand it over.
      write_ms       how long the blocking device write took.
      gap_ms         time from the end of the previous write to the start of
                     this one. A gap longer than the audio already queued in
                     the device is a gap the speaker had to fill with silence.
    """
    record = {
        "kind": "audio_out",
        "latency_ms": round(float(stream_latency) * 1000, 1),
        "frames": int(frames),
        "underflow": bool(underflow),
    }
    if queued_chunks is not None:
        record["queued"] = int(queued_chunks)
    if write_ms is not None:
        record["write_ms"] = round(float(write_ms), 1)
    if gap_ms is not None:
        record["gap_ms"] = round(float(gap_ms), 1)
    _write(record)


def log_audio_in_status(overflow: bool) -> None:
    _write({"kind": "audio_in", "overflow": bool(overflow)})


@contextmanager
def trace_block(name: str, *, warn_ms: float = 150.0):
    """Wrap a suspect call — an AppleScript/osascript call, a notification
    post, a memory load, a reconnect, a plugin-discovery pass — and record
    only its duration. The `slow` flag is what's worth grepping for first;
    on the asyncio event loop used here, anything over ~100-150ms is long
    enough to be felt as a stutter in speech or a delayed state change."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            dt_ms = (time.monotonic() - t0) * 1000
            _write({
                "kind": "block", "name": name,
                "ms": round(dt_ms, 1), "slow": dt_ms > warn_ms,
            })
        except Exception:
            pass


def log_reconnect(reason: str, kept_context: bool, duration_s: float) -> None:
    """`reason` is a coarse label (see core.live_session.classify_disconnect),
    never an error message. `duration_s` is the time without a live session."""
    _write({
        "kind": "reconnect", "reason": str(reason)[:48],
        "kept_context": bool(kept_context),
        "ms": round(float(duration_s) * 1000, 1),
    })


class HeartbeatLag:
    """Notices when a thread is scheduled much later than it asked to be.

    A timer is set to fire every `interval` seconds on the thread being
    watched. Each firing calls tick(). If a firing arrives later than the
    interval by at least `threshold`, that thread was either busy with
    something else or throttled by the OS, and tick() returns the overshoot.

    This is the direct measurement of "something is blocking the event loop /
    the Qt thread", whatever the something is. It carries no content.
    """

    def __init__(self, interval: float, threshold: float):
        self.interval = float(interval)
        self.threshold = float(threshold)
        self._last: float | None = None

    def tick(self, now: float) -> float | None:
        last, self._last = self._last, float(now)
        if last is None:
            return None
        overshoot = (self._last - last) - self.interval
        return overshoot if overshoot >= self.threshold else None


def log_lag(source: str, overshoot_s: float, visible: bool | None = None) -> None:
    """Record one scheduling stall. `source` is "asyncio" or "gui".

    `visible` (GUI only) says whether the window could be seen. A stall that
    only happens while the window is hidden or covered points at macOS
    throttling background apps (App Nap) rather than at Jarvis's own work.
    """
    record = {"kind": "lag", "source": str(source)[:12],
              "ms": round(float(overshoot_s) * 1000, 1)}
    if visible is not None:
        record["visible"] = bool(visible)
    _write(record)


def _isatty() -> bool:
    try:
        return bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        return False
