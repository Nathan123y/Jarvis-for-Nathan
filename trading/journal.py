"""Local record of what the paper trader saw and did, plus its pause switch and run lock.

Everything lives in config/trading_day/ (ignored by git). It holds numbers, ticker
symbols and short labels, plus, when the pre-market analyst is on, its short written plan
(outlook, picks and reasons, in the model's words). No keys, no account numbers, nothing from
your conversations. Delete the folder at any time to start the experiment over.

    journal.jsonl   append-only events (decisions, orders, rejections, snapshots, errors)
    state.json      start line for the comparison, last rebalance date, last-seen account
    PAUSE           exists = trading is paused (the runner keeps watching but sends no orders)
    runner.pid      which process is the live runner, so there is never a second one
"""
from __future__ import annotations

import json
import os
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def default_dir() -> Path:
    from memory.config_manager import CONFIG_DIR
    return CONFIG_DIR / "trading_day"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else default_dir()
        self.events_path = self.dir / "journal.jsonl"
        self.state_path = self.dir / "state.json"
        self.pause_path = self.dir / "PAUSE"
        self.pid_path = self.dir / "runner.pid"
        self.log_path = self.dir / "runner.log"

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── events ────────────────────────────────────────────────────────────────
    def record(self, kind: str, **fields) -> None:
        """Append one event. Journalling must never break trading, so it never raises."""
        try:
            self._ensure()
            line = json.dumps({"t": _now(), "kind": kind, **fields}, separators=(",", ":"))
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    def events(self, kind: Optional[str] = None, limit: int = 5000) -> list[dict]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and (kind is None or event.get("kind") == kind):
                out.append(event)
        return out

    # ── state ─────────────────────────────────────────────────────────────────
    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update_state(self, **fields) -> dict:
        data = self.state()
        data.update(fields)
        try:
            self._ensure()
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="state.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
            os.replace(tmp, self.state_path)
        except OSError:
            pass
        return data

    def orders_today(self, day: str) -> tuple[int, frozenset]:
        """(orders sent today, symbols bought today). Yesterday's tally is ignored."""
        entry = self.state().get("orders", {})
        if not isinstance(entry, dict) or entry.get("day") != day:
            return 0, frozenset()
        return int(entry.get("count", 0)), frozenset(entry.get("bought", []))

    def note_order(self, day: str, symbol: str, side: str) -> None:
        count, bought = self.orders_today(day)
        if side == "buy":
            bought = bought | {symbol}
        self.update_state(orders={"day": day, "count": count + 1, "bought": sorted(bought)})

    # ── pause switch ──────────────────────────────────────────────────────────
    def paused(self) -> bool:
        return self.pause_path.exists()

    def pause(self) -> None:
        self._ensure()
        self.pause_path.write_text(_now(), encoding="utf-8")

    def resume(self) -> None:
        try:
            self.pause_path.unlink()
        except FileNotFoundError:
            pass

    # ── single-runner lock ────────────────────────────────────────────────────
    def runner_pid(self) -> Optional[int]:
        """PID of the live runner, or None (a stale or foreign pid file counts as none)."""
        try:
            pid = int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return pid if _is_our_runner(pid) else None

    def claim_runner(self) -> bool:
        existing = self.runner_pid()
        if existing is not None and existing != os.getpid():
            return False
        self._ensure()
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release_runner(self) -> None:
        try:
            if int(self.pid_path.read_text(encoding="utf-8").strip()) == os.getpid():
                self.pid_path.unlink()
        except (OSError, ValueError):
            pass

    def stop_runner(self) -> bool:
        pid = self.runner_pid()
        if pid is None or pid == os.getpid():
            return False
        try:
            try:
                import psutil
                psutil.Process(pid).terminate()
            except ImportError:
                if os.name == "nt":
                    return False
                os.kill(pid, signal.SIGTERM)
        except Exception:
            return False
        return True


def _is_our_runner(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        try:
            command = " ".join(psutil.Process(pid).cmdline()).lower()
        except Exception:
            return True
        return "trading" in command
    except ImportError:
        if os.name == "nt":
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
