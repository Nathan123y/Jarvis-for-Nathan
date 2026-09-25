"""Reset only Jarvis's selected macOS privacy decisions after quitting the app.

Run on the Mac from the Jarvis project folder, for example:
    python3 tools/reset_macos_access.py all

macOS will ask for permission again when Jarvis next uses each protected feature.
This tool does not grant permissions or modify macOS's privacy database directly.
"""
from __future__ import annotations

import argparse
import platform
import plistlib
import subprocess
from pathlib import Path


BUNDLE_ID = "com.nathan.jarvis.launcher"
SERVICES = {
    "controls": ("Accessibility", "AppleEvents"),
    "data": (
        "AppleEvents", "SystemPolicyDesktopFolder", "SystemPolicyDocumentsFolder",
        "SystemPolicyDownloadsFolder",
    ),
    "all": (
        "Accessibility", "AppleEvents", "SystemPolicyDesktopFolder",
        "SystemPolicyDocumentsFolder", "SystemPolicyDownloadsFolder",
    ),
}


def reset(scope: str, app: Path) -> list[str]:
    if platform.system() != "Darwin":
        raise ValueError("Run this on the Mac where Jarvis is installed.")
    if scope not in SERVICES:
        raise ValueError("Choose controls, data, or all.")
    app = app.expanduser().resolve()
    info_file = app / "Contents" / "Info.plist"
    try:
        with info_file.open("rb") as file:
            info = plistlib.load(file)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"Could not read the installed Jarvis app at {app}.") from exc
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError(f"The app at {app} is not the Jarvis launcher. No access was reset.")
    launcher = app / "Contents" / "MacOS" / "Jarvis"
    if not launcher.is_file():
        raise ValueError("Jarvis's launcher is missing. No access was reset.")
    verified = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", str(app)],
        capture_output=True, text=True, check=False,
    )
    if verified.returncode:
        raise ValueError("Jarvis's app signature is invalid. No access was reset.")
    running = subprocess.run(
        ["/bin/ps", "-axo", "command="], capture_output=True, text=True, check=False,
    )
    if running.returncode or any(
        line.strip().startswith(str(launcher)) for line in running.stdout.splitlines()
    ):
        raise ValueError("Quit Jarvis completely, including its menu bar icon, before resetting access.")

    results = []
    for service in SERVICES[scope]:
        response = subprocess.run(
            ["/usr/bin/tccutil", "reset", service, BUNDLE_ID],
            capture_output=True, text=True, check=False,
        )
        results.append(f"{service}: {'reset' if response.returncode == 0 else 'could not reset'}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset Jarvis's macOS controls/data permissions for a fresh prompt."
    )
    parser.add_argument("scope", choices=SERVICES)
    parser.add_argument("--app", type=Path, default=Path.home() / "Applications" / "Jarvis.app",
                        help="Path to the installed Jarvis.app (default: ~/Applications/Jarvis.app)")
    args = parser.parse_args()
    try:
        results = reset(args.scope, args.app)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for line in results:
        print(line)
    print("Screen Recording and Microphone were not reset.")
    print("Reopen Jarvis and retry the affected feature. Allow any macOS prompt it shows.")
    if args.scope in ("controls", "all"):
        print("For Accessibility, check System Settings → Privacy & Security → Accessibility.")
    if any(line.endswith("could not reset") for line in results):
        parser.exit(1, "Some resets failed. Check the results above; no other services were changed.\n")


if __name__ == "__main__":
    main()
