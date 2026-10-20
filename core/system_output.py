"""
macOS system sound output: which speakers everything else (Chrome, YouTube
Music, videos) plays through.

Jarvis's own voice is opened on a named device (see core/audio_devices.py), so
moving the system output to other speakers leaves Jarvis talking where it was.

This talks to Core Audio directly through ctypes rather than PortAudio, because
PortAudio lists devices once when it starts: speakers switched on later would
never show up, and "are my speakers on" would keep answering from the past.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys


def _code(four: str) -> int:
    return int.from_bytes(four.encode("ascii"), "big")


_DEVICES = _code("dev#")
_DEFAULT_OUTPUT = _code("dOut")
_NAME = _code("lnam")
_STREAMS = _code("stm#")
_GLOBAL = _code("glob")
_OUTPUT_SCOPE = _code("outp")
_SYSTEM_OBJECT = 1
_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


_lib = None


def _ca():
    """Core Audio and Core Foundation, loaded on first use (macOS only)."""
    global _lib
    if _lib is None:
        if sys.platform != "darwin":
            raise OSError("Switching speakers is only available on macOS.")
        ca = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p]
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                          ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _lib = (ca, cf)
    return _lib


def _size(obj: int, addr: _Address) -> int:
    ca, _ = _ca()
    size = ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return 0
    return size.value


def _device_ids() -> list[int]:
    ca, _ = _ca()
    addr = _Address(_DEVICES, _GLOBAL, 0)
    size = _size(_SYSTEM_OBJECT, addr)
    count = size // 4
    ids = (ctypes.c_uint32 * count)()
    n = ctypes.c_uint32(size)
    if count == 0 or ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                                    ctypes.byref(n), ids):
        return []
    return list(ids)


def _name(device: int) -> str:
    ca, cf = _ca()
    addr = _Address(_NAME, _GLOBAL, 0)
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                     ctypes.byref(ref)) or not ref.value:
        return ""
    buf = ctypes.create_string_buffer(256)
    ok = cf.CFStringGetCString(ref, buf, len(buf), _UTF8)
    cf.CFRelease(ref)
    return buf.value.decode("utf-8") if ok else ""


def _has_output(device: int) -> bool:
    return _size(device, _Address(_STREAMS, _OUTPUT_SCOPE, 0)) > 0


def outputs() -> dict[str, int]:
    """Every device that can play sound right now, by name."""
    return {name: d for d in _device_ids() if _has_output(d) and (name := _name(d))}


def current() -> str:
    """Name of the system output device."""
    ca, _ = _ca()
    addr = _Address(_DEFAULT_OUTPUT, _GLOBAL, 0)
    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    if ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                     ctypes.byref(size), ctypes.byref(dev)):
        return ""
    return _name(dev.value)


def find(name: str) -> str | None:
    """The connected output whose name matches `name` (case-insensitive,
    also as a prefix), or None when it isn't connected."""
    want = name.strip().lower()
    names = list(outputs())
    return next((n for n in names if n.lower() == want), None) or \
        next((n for n in names if n.lower().startswith(want)), None)


def switch_to(name: str) -> bool:
    """Make `name` the system output. False if it isn't connected."""
    ca, _ = _ca()
    match = find(name)
    if match is None:
        return False
    dev = ctypes.c_uint32(outputs()[match])
    addr = _Address(_DEFAULT_OUTPUT, _GLOBAL, 0)
    return ca.AudioObjectSetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                         4, ctypes.byref(dev)) == 0
