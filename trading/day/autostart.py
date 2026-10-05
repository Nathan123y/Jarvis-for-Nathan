"""Start the day trader by itself every weekday morning (macOS only).

`python3 -m trading.day autostart install` writes a small launchd job into the user's own
LaunchAgents folder (no admin rights needed). launchd then starts the trader Monday to Friday at the
chosen time of this Mac's clock, in the background, with the Mac kept from idle-sleeping while it
runs. Nothing is sent anywhere and no key is written into the job: the trader reads its keys from
Jarvis Plugin Settings exactly as when started by hand.

What it cannot do, and says so: wake a sleeping Mac (that needs one `sudo pmset` line the user
runs themselves) or run with the Mac off. A start that falls while the Mac sleeps happens when it wakes.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

LABEL = "com.jarvis.daytrader"
WEEKDAYS = (1, 2, 3, 4, 5)                  # launchd: 1 = Monday ... 5 = Friday
DEFAULT_AT = "06:30"                        # the Mac's clock; 6:30 am Pacific is the New York open


def parse_time(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"The start time must look like 06:30 (24-hour clock), not {text!r}.")
    return int(match.group(1)), int(match.group(2))


def plist_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(*, python: str, repo: Path, log: Path, run_flags: list[str], at: str = DEFAULT_AT) -> dict:
    hour, minute = parse_time(at)
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", python, "-u", "-m", "trading.day", "run", *run_flags],
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": [{"Weekday": d, "Hour": hour, "Minute": minute} for d in WEEKDAYS],
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"},
    }


def wake_command(at: str = DEFAULT_AT) -> str:
    hour, minute = parse_time(at)
    minute -= 5
    if minute < 0:
        hour, minute = (hour - 1) % 24, minute + 60
    return f"sudo pmset repeat wakeorpoweron MTWRF {hour:02d}:{minute:02d}:00"


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def install(*, python: str, repo: Path, log: Path, run_flags: list[str], at: str = DEFAULT_AT,
            home: Optional[Path] = None, uid: Optional[int] = None,
            launchctl: Callable[[list[str]], subprocess.CompletedProcess] = _launchctl) -> list[str]:
    """Write the job and load it. Returns plain-language lines to show the user."""
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(python=python, repo=repo, log=log, run_flags=run_flags, at=at), handle)
    domain = f"gui/{os.getuid() if uid is None else uid}"
    launchctl(["bootout", f"{domain}/{LABEL}"])                  # replace an older copy; fine if there was none
    result = launchctl(["bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError("macOS refused to load the start-up job: "
                           + ((result.stderr or result.stdout or "").strip()[:200] or f"exit {result.returncode}"))
    return [f"Installed. The day trader will start by itself Monday to Friday at {at} (this Mac's clock).",
            f"It runs: trading.day run {' '.join(run_flags)}".rstrip(),
            f"Its log is {log}.",
            "To wake a sleeping Mac a few minutes before, run this once yourself (it asks for your password):",
            f"  {wake_command(at)}"]


def remove(*, home: Optional[Path] = None, uid: Optional[int] = None,
           launchctl: Callable[[list[str]], subprocess.CompletedProcess] = _launchctl) -> list[str]:
    domain = f"gui/{os.getuid() if uid is None else uid}"
    launchctl(["bootout", f"{domain}/{LABEL}"])
    path = plist_path(home)
    existed = path.exists()
    if existed:
        path.unlink()
    return ["Removed. The day trader will no longer start by itself." if existed else
            "It was not installed. Nothing to remove.",
            "If you set a wake time, `sudo pmset repeat cancel` clears it."]


def status(*, home: Optional[Path] = None, uid: Optional[int] = None,
           launchctl: Callable[[list[str]], subprocess.CompletedProcess] = _launchctl) -> tuple[bool, bool]:
    """(installed file exists, loaded in launchd)."""
    domain = f"gui/{os.getuid() if uid is None else uid}"
    loaded = launchctl(["print", f"{domain}/{LABEL}"]).returncode == 0
    return plist_path(home).exists(), loaded


def current_python() -> str:
    return sys.executable
