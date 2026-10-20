"""Turn the user's Govee LED strip (H6139) on and off, set its colour and
brightness, and read its state, over Bluetooth LE from this Mac.

Each command connects, writes and disconnects, because the strip accepts one
connection at a time and the Govee phone app may want it next. Every frame is
20 bytes: the command padded with zeros to 19 bytes, then an XOR of those 19.
These frames were each checked on the user's H6139 before being used here:
  power      33 01 01 (on) / 33 01 00 (off)
  brightness 33 04 XX, where XX runs 00-FF (homebridge-govee's default scale;
             0x7F visibly dimmed the strip to about half)
  colour     33 05 02 RR GG BB (homebridge-govee's default manual-RGB command)
  state      aa 01 / aa 04 / aa 05, answered on the notify characteristic
"""
from __future__ import annotations

import asyncio
import os
import plistlib
import re
import subprocess
import sys
import threading
from pathlib import Path

from memory.config_manager import get_plugin_config


PLUGIN = {
    "name": "lights",
    "description": (
        "Control the user's LED strip lights (Govee, over Bluetooth): turn them on or "
        "off or toggle them, set a colour, set brightness, or report their state. Use for 'turn on my "
        "lights', 'lights off', 'make the LEDs blue', 'set the lights to 30 percent', "
        "'what colour are my lights'. 'Lighten up' means action brightness at 70. Not for the screen's brightness, which belongs to "
        "the computer settings tool. The strip is found automatically: never ask the "
        "user for a Bluetooth address and never pass one."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "on, off, toggle, color, brightness, or status"},
            "color": {"type": "STRING", "description": "For color: a colour name (red, warm white…), #RRGGBB, or R,G,B"},
            "brightness": {"type": "INTEGER", "description": "For brightness: 1-100 percent"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "lights",
    "title": "Govee LED strip (Bluetooth)",
    "fields": [{"key": "address", "type": "text", "label": "Pinned Bluetooth address (optional)",
                "placeholder": "Leave blank to find the strip by its name"},
               {"key": "clap", "type": "text", "label": "Double clap or snap switches the lights on and off (on or off)",
                "placeholder": "on"}],
}

_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"
_NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
_NAME_HINTS = ("h6139", "govee", "ihoment", "minger")
_SCAN_SECONDS = 6
_CONNECT_SECONDS = 8
_TOTAL_SECONDS = 12
# The strip ignores a query sent the moment notifications are switched on.
_NOTIFY_SETTLE = 0.5

_COLORS = {
    "red": (255, 0, 0), "orange": (255, 100, 0), "yellow": (255, 200, 0),
    "green": (0, 255, 0), "cyan": (0, 255, 255), "blue": (0, 0, 255),
    "purple": (128, 0, 255), "violet": (128, 0, 255), "magenta": (255, 0, 255),
    "pink": (255, 60, 150), "white": (255, 255, 255), "warm white": (255, 160, 60),
}

_lock = threading.Lock()   # one Bluetooth connection at a time


def _frame(command: bytes) -> bytes:
    body = command.ljust(19, b"\0")
    check = 0
    for byte in body:
        check ^= byte
    return body + bytes([check])


def _power_frame(on: bool) -> bytes:
    return _frame(bytes([0x33, 0x01, 0x01 if on else 0x00]))


def _power_state(answers: list[bytes]) -> bool | None:
    """The strip's reply to a power query (aa 01 ..), if one came back."""
    for a in answers:
        if len(a) >= 3 and a[0] == 0xAA and a[1] == 0x01:
            return bool(a[2])
    return None


def _brightness_frame(percent: int) -> bytes:
    return _frame(bytes([0x33, 0x04, max(1, int(percent / 100 * 0xFF))]))


def _color_frame(rgb: tuple[int, int, int]) -> bytes:
    return _frame(bytes([0x33, 0x05, 0x02, *rgb]))


def _parse_color(text) -> tuple[int, int, int] | None:
    value = " ".join(str(text or "").lower().replace("colour", "").split())
    if value in _COLORS:
        return _COLORS[value]
    if m := re.fullmatch(r"#?([0-9a-f]{6})", value):
        return tuple(bytes.fromhex(m.group(1)))
    parts = re.findall(r"\d+", value)
    if len(parts) == 3 and re.fullmatch(r"(rgb)?\s*\(?\s*\d+\s*[, ]\s*\d+\s*[, ]\s*\d+\s*\)?", value):
        rgb = tuple(int(p) for p in parts)
        if all(0 <= c <= 255 for c in rgb):
            return rgb
    return None


def _color_name(rgb) -> str:
    for name, value in _COLORS.items():
        if value == tuple(rgb):
            return name
    return "#%02x%02x%02x" % tuple(rgb)


def _bluetooth_blocked() -> bool:
    """macOS kills an app that touches Bluetooth without a usage description,
    and under Jarvis.app that would take the whole voice session down. Inside
    the app, refuse until its Info.plist declares one."""
    if sys.platform != "darwin" or not os.environ.get("JARVIS_MENU_CONTROL_DIR"):
        return False
    try:
        parent = subprocess.run(["/bin/ps", "-o", "comm=", "-p", str(os.getppid())],
                                capture_output=True, text=True, timeout=3).stdout.strip()
        if ".app/Contents/MacOS/" not in parent:
            return False
        info = Path(parent.split(".app/Contents/MacOS/")[0] + ".app") / "Contents" / "Info.plist"
        with info.open("rb") as source:
            return "NSBluetoothAlwaysUsageDescription" not in plistlib.load(source)
    except Exception:
        return False


class _Bleak:
    """The real Bluetooth backend. Tests replace it with a fake."""

    async def find(self, address: str | None):
        from bleak import BleakScanner
        if address:
            return await BleakScanner.find_device_by_address(address, timeout=_SCAN_SECONDS)
        return await BleakScanner.find_device_by_filter(
            lambda d, adv: any(h in (adv.local_name or d.name or "").lower() for h in _NAME_HINTS),
            timeout=_SCAN_SECONDS)

    async def exchange(self, device, frames: list[bytes], replies: bool = False) -> list[bytes]:
        from bleak import BleakClient
        answers: list[bytes] = []
        async with BleakClient(device, timeout=_CONNECT_SECONDS) as client:
            if replies:
                await client.start_notify(_NOTIFY, lambda _h, data: answers.append(bytes(data)))
                await asyncio.sleep(_NOTIFY_SETTLE)
            for frame in frames:
                await client.write_gatt_char(_WRITE, frame, response=False)
                await asyncio.sleep(0.6 if replies else 0.15)
        return answers

    async def toggle(self, device) -> bool:
        """Ask whether the strip is on and send the opposite, in one connection,
        so it is right even after the phone app or a voice command changed it.
        Returns True if the strip is now on. No answer counts as off."""
        from bleak import BleakClient
        answers: list[bytes] = []
        async with BleakClient(device, timeout=_CONNECT_SECONDS) as client:
            await client.start_notify(_NOTIFY, lambda _h, data: answers.append(bytes(data)))
            await asyncio.sleep(_NOTIFY_SETTLE)
            await client.write_gatt_char(_WRITE, _frame(bytes([0xAA, 0x01])), response=False)
            for _ in range(15):
                if _power_state(answers) is not None:
                    break
                await asyncio.sleep(0.1)
            on = _power_state(answers) is not True
            await client.write_gatt_char(_WRITE, _power_frame(on), response=False)
            await asyncio.sleep(0.15)
        return on


def _backend():
    return _Bleak()


async def _send(work):
    """Find the strip, then run work(backend, device). None if not found."""
    backend = _backend()
    address = str(get_plugin_config("lights").get("address") or "").strip() or None
    device = await backend.find(address)
    if device is None:
        return None
    return await work(backend, device)


def _status_text(answers: list[bytes]) -> str:
    power = brightness = color = None
    for a in answers:
        if len(a) < 3 or a[0] != 0xAA:
            continue
        if a[1] == 0x01:
            power = "on" if a[2] else "off"
        elif a[1] == 0x04:
            brightness = round(a[2] * 100 / 0xFF)
        elif a[1] == 0x05 and a[2] == 0x02 and len(a) >= 6:
            color = _color_name(a[3:6])
    if power is None and brightness is None and color is None:
        return "I reached the LED strip, but it didn't report its state."
    parts = [f"The LED strip is {power}" if power else "The LED strip is reachable"]
    if brightness is not None:
        parts.append(f"at {brightness} percent brightness")
    if color:
        parts.append(f"set to {color}")
    return ", ".join(parts) + "."


def _log(player, text):
    if player:
        try:
            player.write_log(f"JARVIS: {text}")
        except Exception:
            pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").strip().lower()
    if action in ("colour", "set_color"):
        action = "color"

    if action == "on":
        frames, done = [_power_frame(True)], "Your LED lights are on."
    elif action == "off":
        frames, done = [_power_frame(False)], "Your LED lights are off."
    elif action == "toggle":
        frames, done = [], ""
    elif action == "color":
        rgb = _parse_color(params.get("color"))
        if rgb is None:
            return ("I didn't catch which colour. Say a colour name like blue or warm white, "
                    "or an RGB value.")
        frames, done = [_power_frame(True), _color_frame(rgb)], f"Your LED lights are {_color_name(rgb)}."
    elif action == "brightness":
        try:
            percent = int(float(params.get("brightness")))
        except (TypeError, ValueError, OverflowError):
            percent = 0
        if not 1 <= percent <= 100:
            return "Brightness needs to be between 1 and 100 percent."
        frames, done = ([_power_frame(True), _brightness_frame(percent)],
                        f"Your LED lights are at {percent} percent.")
    elif action == "status":
        frames = [_frame(bytes([0xAA, code])) for code in (0x01, 0x04, 0x05)]
        done = ""
    else:
        return "LED commands are on, off, toggle, color, brightness, or status."

    if _bluetooth_blocked():
        return ("Jarvis can't use Bluetooth yet: the Jarvis app needs a Bluetooth permission "
                "entry and a rebuild first. Until then I've left the lights alone.")
    if not _lock.acquire(timeout=_TOTAL_SECONDS):
        return "I'm still talking to the LED strip. Try again in a moment."
    try:
        if action == "toggle":
            work = lambda backend, device: backend.toggle(device)
        else:
            work = lambda backend, device: backend.exchange(device, frames, action == "status")
        answers = asyncio.run(asyncio.wait_for(_send(work), _TOTAL_SECONDS))
    except ImportError:
        return "Bluetooth support isn't installed. Run: pip install bleak"
    except asyncio.TimeoutError:
        return "The LED strip didn't answer in time. Make sure it's on and close by, then try again."
    except Exception as exc:
        text = str(exc).lower()
        if "powered off" in text or "bluetooth is off" in text or "turned off" in text:
            return "Bluetooth is off on this Mac. Turn it on and try again."
        if "unauthorized" in text or "not authorized" in text:
            return "Jarvis isn't allowed to use Bluetooth. Allow it in System Settings, Privacy & Security, Bluetooth."
        return "I couldn't reach the LED strip. If your phone's Govee app is connected to it, close the app and try again."
    finally:
        _lock.release()

    if answers is None:
        return ("I couldn't find your LED strip. Make sure it's plugged in and that your "
                "phone isn't connected to it.")
    if action == "toggle":
        result = "Your LED lights are on." if answers else "Your LED lights are off."
    else:
        result = _status_text(answers) if action == "status" else done
    _log(player, result)
    return result
