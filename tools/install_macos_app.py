"""Create a local Finder launcher for the Jarvis Git checkout.

Run with the same Python interpreter you use for `python3 main.py`:
    python3 tools/install_macos_app.py

The app contains only a launcher. It does not copy source, dependencies or secrets.
"""
from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path


BUNDLE_ID = "com.nathan.jarvis.launcher"
MARKER = "# Jarvis Git checkout launcher"
NATIVE_SOURCE = Path(__file__).with_name("jarvis_launcher.swift")
ANNOUNCER_SOURCE = Path(__file__).with_name("notification_announcer.swift")


def compatible_arch(interpreter: Path) -> str:
    """Match macOS Python to its installed native audio extension."""
    for arch in ("arm64", "x86_64"):
        try:
            result = subprocess.run(
                ["/usr/bin/arch", f"-{arch}", str(interpreter), "-c",
                 "import sounddevice, numpy, PyQt6.QtWidgets"],
                capture_output=True, text=True, timeout=12, check=False,
            )
            if result.returncode == 0:
                return arch
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise ValueError("This Python cannot import Jarvis's audio or UI dependencies on either Mac architecture. "
                     "Run the installer with the Python that successfully runs main.py.")


def relocate_project(repo: Path, destination: Path) -> Path:
    """Move the one Git checkout intact, including ignored credentials and settings."""
    repo, destination = repo.resolve(), destination.expanduser()
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}. No files were moved.")
    if not (repo / "main.py").is_file() or not (repo / ".git").exists():
        raise ValueError("Cannot find the Jarvis Git checkout. No files were moved.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(shutil.move(str(repo), str(destination))).resolve()


def install(repo: Path, interpreter: Path, destination: Path, architecture: str | None = None) -> Path:
    repo = repo.expanduser().resolve()
    interpreter = interpreter.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not (repo / "main.py").is_file():
        raise ValueError(f"Cannot find main.py in {repo}")
    if not interpreter.is_file():
        raise ValueError(f"Python interpreter does not exist: {interpreter}")
    if destination.suffix != ".app":
        raise ValueError("Destination must end in .app")
    if architecture not in (None, "arm64", "x86_64"):
        raise ValueError("Invalid Python architecture")
    if destination.exists():
        info = destination / "Contents" / "Info.plist"
        try:
            with info.open("rb") as source:
                existing = plistlib.load(source)
            native_config = destination / "Contents" / "Resources" / "launch.plist"
            old_launcher = destination / "Contents" / "MacOS" / "Jarvis"
            is_ours = native_config.is_file() or (old_launcher.is_file() and
                        MARKER in old_launcher.read_text(errors="replace"))
            if existing.get("CFBundleIdentifier") != BUNDLE_ID or not is_ours:
                raise ValueError("An unrelated app exists at that destination; choose another path.")
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
            raise ValueError("An unrelated app exists at that destination; choose another path.") from exc

    macos = destination / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "Jarvis",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Jarvis",
        "CFBundleDisplayName": "Jarvis",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSMicrophoneUsageDescription": "Jarvis listens when you use its voice assistant.",
        "NSCameraUsageDescription": "Jarvis accesses the camera when you request a camera feature.",
        "NSAppleEventsUsageDescription": "Jarvis controls approved Mac apps when you request an action.",
    }
    with (destination / "Contents" / "Info.plist").open("wb") as target:
        plistlib.dump(info, target)

    if platform.system() == "Darwin":
        # A shell-script bundle doesn't itself request microphone permission.
        # A native executable provides a stable app identity and prompts before
        # starting the Python process that captures audio.
        config = destination / "Contents" / "Resources"
        config.mkdir(parents=True, exist_ok=True)
        with (config / "launch.plist").open("wb") as target:
            plistlib.dump({"Repo": str(repo), "Python": str(interpreter),
                           "Arch": architecture or ""}, target)
        launcher = destination / "Contents" / "MacOS" / "Jarvis"
        result = subprocess.run(["/usr/bin/xcrun", "swiftc", str(NATIVE_SOURCE), str(ANNOUNCER_SOURCE),
                                 "-framework", "AVFoundation", "-framework", "AppKit",
                                 "-framework", "ApplicationServices",
                                 "-framework", "ScreenCaptureKit",
                                 "-o", str(launcher)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Could not compile Jarvis launcher: {result.stderr.strip()}")
        result = subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-",
                                 str(destination)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"Could not sign Jarvis launcher: {result.stderr.strip()}")
        return destination

    script = f"""#!/bin/sh
{MARKER}
REPO={shlex.quote(str(repo))}
PYTHON={shlex.quote(str(interpreter))}
ARCH={shlex.quote(architecture or '')}
export PATH={shlex.quote(str(interpreter.parent))}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
LOG_DIR="$HOME/Library/Logs/Jarvis"
umask 077
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/launch.log"
if [ ! -f "$REPO/main.py" ] || [ ! -x "$PYTHON" ]; then
    echo "Jarvis cannot start: checkout or Python moved. Run the installer again." >> "$LOG"
    /usr/bin/osascript -e 'display alert "Jarvis cannot start" message "Your project folder or Python moved. Run the Jarvis app installer again."' >/dev/null 2>&1
    exit 1
fi
cd "$REPO" || exit 1
if [ -n "$ARCH" ]; then
    /usr/bin/arch "-$ARCH" "$PYTHON" "$REPO/main.py" >> "$LOG" 2>&1
else
    "$PYTHON" "$REPO/main.py" >> "$LOG" 2>&1
fi
status=$?
if [ "$status" -ne 0 ]; then
    /usr/bin/osascript -e 'display alert "Jarvis could not start" message "See Library/Logs/Jarvis/launch.log for the startup error."' >/dev/null 2>&1
fi
exit "$status"
"""
    launcher = macos / "Jarvis"
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a clickable Jarvis.app linked to this Git checkout")
    parser.add_argument("--destination", type=Path, default=Path.home() / "Applications" / "Jarvis.app")
    parser.add_argument("--relocate-project", action="store_true",
                        help="Move the Git checkout from Desktop to ~/Projects before reinstalling")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Run this installer on the Mac where you use Jarvis.")
    repo = Path(__file__).resolve().parents[1]
    try:
        arch = compatible_arch(Path(sys.executable))
        if args.relocate_project:
            repo = relocate_project(repo, Path.home() / "Projects" / repo.name)
        target = install(repo, Path(sys.executable), args.destination, architecture=arch)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Installed {target}")
    print(f"Launch it from Finder or Spotlight. It runs {repo / 'main.py'} ({arch})")
    if args.relocate_project:
        print(f"Reopen {repo} in VS Code for future Git pulls; your config moved with it.")
    print("Future Git pulls update the code it launches; quit and reopen Jarvis to use new code.")
    print("Launch errors are in ~/Library/Logs/Jarvis/launch.log")


if __name__ == "__main__":
    main()
