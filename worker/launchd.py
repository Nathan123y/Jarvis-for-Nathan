"""Per-user launchd job that keeps the Jarvis background worker running (macOS).

A LaunchAgent lives in ~/Library/LaunchAgents and needs no admin rights. What that means:

  * It starts when YOU log in (RunAtLoad) and is restarted if it crashes (not if you stop it).
  * It runs in your login session. After you log out, or before anyone has logged in after a
    restart, it is not running. A locked screen or a closed Jarvis window is fine: the worker uses
    saved sign-ins (no windows, no clicking, no microphone).
  * A sleeping Mac runs nothing; it carries on after wake, without replaying expired outreach.
    A Mac that is off or out of battery does nothing.
  * Nothing here changes power settings. Staying awake is a separate opt-in (see runtime.KeepAwake).

`install` keeps the previous job file as a backup so `rollback` can put it back.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

LABEL = "com.jarvis.worker"
Launchctl = Callable[[list[str]], subprocess.CompletedProcess]

SESSION_NOTES = [
    "It runs while you are logged in to this Mac, with or without the Jarvis window, and with the screen locked.",
    "It does nothing while the Mac sleeps (it resumes after wake) or is off, and after you log out.",
]


def plist_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def backup_path(home: Optional[Path] = None) -> Path:
    p = plist_path(home)
    return p.with_name(p.name + ".prev")


def build_plist(*, python: str, repo: Path, log: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-u", "-m", "worker", "run"],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},      # restart after a crash, not after a clean stop
        "ThrottleInterval": 30,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"},
    }


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _domain(uid: Optional[int]) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _load(path: Path, uid, launchctl: Launchctl) -> None:
    domain = _domain(uid)
    launchctl(["bootout", f"{domain}/{LABEL}"])            # replace an older copy; fine if there was none
    result = launchctl(["bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError("macOS refused to load the worker job: "
                           + ((result.stderr or result.stdout or "").strip()[:200] or f"exit {result.returncode}"))


def install(*, python: Optional[str] = None, repo: Path, log: Path, home: Optional[Path] = None,
            uid: Optional[int] = None, launchctl: Launchctl = _launchctl) -> list[str]:
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, backup_path(home))                # so `rollback` can restore it
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(python=python or sys.executable, repo=repo, log=log), fh)
    _load(path, uid, launchctl)
    return ["Installed. The Jarvis background worker now starts when you log in and restarts if it crashes.",
            f"It runs: {python or sys.executable} -m worker run   (in {repo})",
            f"Its log is {log}.", *SESSION_NOTES,
            "Undo with `python3 -m worker uninstall`; go back to the previous job file with `rollback`."]


def uninstall(*, home: Optional[Path] = None, uid: Optional[int] = None,
              launchctl: Launchctl = _launchctl) -> list[str]:
    launchctl(["bootout", f"{_domain(uid)}/{LABEL}"])
    path, existed = plist_path(home), plist_path(home).exists()
    if existed:
        path.unlink()
    return ["Removed. The worker will no longer start by itself." if existed else "It was not installed."]


def restart(*, uid: Optional[int] = None, launchctl: Launchctl = _launchctl) -> list[str]:
    result = launchctl(["kickstart", "-k", f"{_domain(uid)}/{LABEL}"])
    if result.returncode != 0:
        raise RuntimeError("Could not restart the worker (is it installed? try `install`): "
                           + ((result.stderr or result.stdout or "").strip()[:200] or f"exit {result.returncode}"))
    return ["Restarted the worker."]


def rollback(*, home: Optional[Path] = None, uid: Optional[int] = None,
             launchctl: Launchctl = _launchctl) -> list[str]:
    """Put the previous job file back (or remove the job if there was none before install)."""
    path, prev = plist_path(home), backup_path(home)
    if not prev.exists():
        return uninstall(home=home, uid=uid, launchctl=launchctl) + ["(There was no earlier version to restore.)"]
    shutil.move(str(prev), str(path))
    _load(path, uid, launchctl)
    return ["Restored the previous worker job and reloaded it."]


def status(*, home: Optional[Path] = None, uid: Optional[int] = None,
           launchctl: Launchctl = _launchctl) -> tuple[bool, bool]:
    """(job file present, loaded in launchd)."""
    loaded = launchctl(["print", f"{_domain(uid)}/{LABEL}"]).returncode == 0
    return plist_path(home).exists(), loaded
