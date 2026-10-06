"""Motion rules for the orb centrepiece, kept free of Qt so they can be tested.

The orb is the user's own artwork (assets/orb.png); the UI only turns, scales
and brightens it. This module decides how fast it turns and how bright it is
for each state of the assistant.
"""
from __future__ import annotations

from pathlib import Path

ORB_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "orb.png"

# Degrees the orb turns per second of animation phase.
_SPIN_IDLE, _SPIN_BUSY, _SPIN_SPEAK, _SPIN_MUTED = 6.0, 22.0, 14.0, 1.5


def orb_spin(state: str, speaking: bool, muted: bool) -> float:
    """Slow when idle or muted, brisk while thinking, steady while speaking."""
    if muted:
        return _SPIN_MUTED
    if state in ("THINKING", "PROCESSING"):
        return _SPIN_BUSY
    if speaking:
        return _SPIN_SPEAK
    return _SPIN_IDLE


def orb_brightness(amp: float, speaking: bool, muted: bool) -> float:
    """0..1 opacity of the artwork. Dim when muted, lifted by the voice level."""
    if muted:
        return 0.30
    amp = max(0.0, min(1.0, float(amp or 0.0)))
    base = 0.82 if speaking else 0.72
    return min(1.0, base + 0.28 * amp)


def orb_scale(base: float, amp: float) -> float:
    """Size multiplier: the breathing base plus a small push from the voice."""
    amp = max(0.0, min(1.0, float(amp or 0.0)))
    return max(0.5, float(base)) * (1.0 + 0.05 * amp)
