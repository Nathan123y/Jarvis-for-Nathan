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
import stat
import sys
from pathlib import Path


BUNDLE_ID = "com.nathan.jarvis.launcher"
MARKER = "# Jarvis Git checkout launcher"


def install(repo: Path, interpreter: Path, destination: Path) -> Path:
    repo = repo.expanduser().resolve()
    interpreter = interpreter.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not (repo / "main.py").is_file():
        raise ValueError(f"Cannot find main.py in {repo}")
    if not interpreter.is_file():
        raise ValueError(f"Python interpreter does not exist: {interpreter}")
    if destination.suffix != ".app":
        raise ValueError("Destination must end in .app")
    if destination.exists():
        info = destination / "Contents" / "Info.plist"
        try:
            with info.open("rb") as source:
                existing = plistlib.load(source)
            if (existing.get("CFBundleIdentifier") != BUNDLE_ID or
                    MARKER not in (destination / "Contents" / "MacOS" / "Jarvis").read_text()):
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

    script = f"""#!/bin/sh
{MARKER}
REPO={shlex.quote(str(repo))}
PYTHON={shlex.quote(str(interpreter))}
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
exec "$PYTHON" "$REPO/main.py" >> "$LOG" 2>&1
"""
    launcher = macos / "Jarvis"
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a clickable Jarvis.app linked to this Git checkout")
    parser.add_argument("--destination", type=Path, default=Path.home() / "Applications" / "Jarvis.app")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Run this installer on the Mac where you use Jarvis.")
    repo = Path(__file__).resolve().parents[1]
    target = install(repo, Path(sys.executable), args.destination)
    print(f"Installed {target}")
    print(f"Launch it from Finder or Spotlight. It runs {repo / 'main.py'}")
    print("Future Git pulls update the code it launches; quit and reopen Jarvis to use new code.")
    print("Launch errors are in ~/Library/Logs/Jarvis/launch.log")


if __name__ == "__main__":
    main()
