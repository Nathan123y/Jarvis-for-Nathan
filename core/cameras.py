"""Choose between the Mac's own camera and the iPhone (Continuity Camera).

OpenCV opens cameras by number. On macOS its numbering is every video device
sorted by the device's unique ID (see OpenCV's cap_avfoundation_mac.mm), so
reading the camera names and unique IDs from `system_profiler` and sorting the
same way tells us which number is the iPhone and which is the built-in camera.

If the names can't be read, the computer camera falls back to the saved or
auto-detected index exactly as before, and the phone camera says plainly that
it can't be found.
"""
from __future__ import annotations

import json
import platform
import subprocess

SOURCES = ("computer", "phone")

PHONE_MISSING = (
    "I can't see your iPhone camera. Keep the iPhone near the Mac, signed in to the "
    "same Apple ID with Wi-Fi and Bluetooth on, locked and held steady (landscape works "
    "best), then ask again. Continuity Camera needs macOS 13 and iOS 16 or later."
)


def normalise(source) -> str | None:
    """'phone' / 'computer' from loose words; None when no camera was named."""
    s = str(source or "").strip().lower()
    if not s:
        return None
    if any(w in s for w in ("phone", "iphone", "mobile", "continuity")):
        return "phone"
    if any(w in s for w in ("computer", "mac", "laptop", "built", "facetime", "webcam", "desktop")):
        return "computer"
    return None


def parse_profiler(text: str) -> list[dict]:
    """[{name, uid}] in OpenCV index order, from `system_profiler SPCameraDataType -json`."""
    try:
        items = json.loads(text).get("SPCameraDataType") or []
    except (ValueError, AttributeError):
        return []
    devices = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("spcamera_unique-id") or "")
        if uid:
            devices.append({"name": str(item.get("_name") or ""), "uid": uid,
                            "model": str(item.get("spcamera_model-id") or "")})
    devices.sort(key=lambda d: d["uid"])
    return devices


def _is_phone(d: dict) -> bool:
    text = f"{d['name']} {d.get('model', '')}".lower()
    return "phone" in text or "continuity" in text     # "iPhone", or a renamed "Nate's Phone"


def pick(devices: list[dict], source: str) -> int | None:
    """OpenCV index for 'phone' or 'computer' in a parsed device list, or None."""
    if source == "phone":
        phones = [i for i, d in enumerate(devices) if _is_phone(d)]
        main = [i for i in phones if "desk view" not in devices[i]["name"].lower()]
        return (main or phones or [None])[0]
    others = [i for i, d in enumerate(devices) if not _is_phone(d)]
    built_in = [i for i in others if any(w in devices[i]["name"].lower()
                                         for w in ("facetime", "built-in", "macbook"))]
    return (built_in or others or [None])[0]


def list_devices() -> list[dict]:
    if platform.system() != "Darwin":
        return []
    try:
        out = subprocess.run(["/usr/sbin/system_profiler", "SPCameraDataType", "-json"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_profiler(out.stdout) if out.returncode == 0 else []


def resolve(source: str | None, fallback_index, saved=None) -> tuple[int, str]:
    """(OpenCV index, source used). `fallback_index` is a callable giving the old
    saved/auto-detected index, used for the computer camera when names are unknown.
    `saved` is a camera index the user's config already names; for the computer
    camera it wins as long as it isn't the phone (e.g. an external webcam).
    Raises RuntimeError with a speakable message when the phone can't be found."""
    source = source if source in SOURCES else "computer"
    devices = list_devices()
    if source == "computer" and devices and saved is not None:
        try:
            s = int(saved)
            if 0 <= s < len(devices) and not _is_phone(devices[s]):
                return s, source
        except (TypeError, ValueError):
            pass
    index = pick(devices, source) if devices else None
    if index is not None:
        return index, source
    if source == "phone":
        raise RuntimeError(PHONE_MISSING)
    return int(fallback_index()), source
