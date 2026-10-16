"""Do-not-disturb: one switch that tells every announcer to hold its news.

Routines turn it on (study mode, goodnight) and off again. While it is on,
the message and package watchers keep what arrives and give a short digest
when it ends, instead of speaking over the user's focus or sleep. It can carry
an end time (goodnight: until the morning), after which it is simply off.
Saved in the config so a restart keeps it.
"""
from __future__ import annotations

import time

from memory.config_manager import _save_flag, load_api_keys


def state() -> dict:
    raw = load_api_keys().get("dnd") or {}
    return raw if isinstance(raw, dict) else {}


def is_on(now: float | None = None) -> bool:
    s = state()
    if not s.get("on"):
        return False
    until = s.get("until") or 0
    return not until or (now or time.time()) < until


def reason() -> str:
    return str(state().get("reason") or "do not disturb") if is_on() else ""


def turn_on(reason: str, until: float | None = None) -> None:
    _save_flag("dnd", {"on": True, "reason": reason, "since": time.time(),
                       "until": float(until) if until else 0})


def turn_off() -> None:
    _save_flag("dnd", {"on": False})
