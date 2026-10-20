"""
Local double-clap / double-snap detection.

The mic callback hands frames to feed(), which only copies them onto a queue,
the same way the wake-word detector is fed; the analysis runs in this module's
own thread so the real-time audio path never slows. Nothing leaves the machine.

A clap or a snap is an impulse: the level jumps far above the room's background
within one 16 ms slice, the sound is bright (mostly high frequencies, unlike a
voice), and it dies away again within about a tenth of a second. Two impulses
0.15-0.8 s apart, with quiet on either side, are a double clap. A third impulse
cancels it, so applause, drumming or typing bursts do not fire.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np

SAMPLE_RATE = 16000
SLICE = 256                 # 16 ms at 16 kHz

# Onset: loud against the background, with a sharp rise from the slice before.
MIN_LEVEL = 0.02            # RMS floor (full scale = 1.0), so silence noise never counts
OVER_BACKGROUND = 6.0       # times the room's running background RMS
RISE = 3.0                  # times the previous slice
MIN_BRIGHTNESS = 0.55       # share of energy in the sample-to-sample difference
# An impulse must fall back under DECAY x its peak within DECAY_WINDOW.
DECAY = 0.3
DECAY_WINDOW = 0.12
# A clap rings on in the room for a moment after it decays; that ring is ignored.
RING = 0.15
# Spacing of the two impulses, and the quiet required around the pair.
MIN_GAP = 0.15
MAX_GAP = 0.8
QUIET_AROUND = 0.6
COOLDOWN = 3.0


def _features(chunk: np.ndarray) -> tuple[float, float]:
    """RMS level and brightness of one slice of float samples in [-1, 1]."""
    rms = float(np.sqrt(np.mean(chunk * chunk)))
    if rms <= 1e-9:
        return 0.0, 0.0
    diff = np.diff(chunk)
    # White noise scores about 1.4, voiced speech well under 0.5.
    brightness = float(np.sqrt(np.mean(diff * diff))) / rms / 1.4
    return rms, brightness


class DoubleClapTracker:
    """Pure signal logic: push float samples, get True when a double clap ends.
    Kept free of threads and queues so it can be tested with synthetic audio."""

    def __init__(self):
        self._background = MIN_LEVEL / OVER_BACKGROUND
        self._previous = 0.0
        self._pending: tuple[float, float] | None = None   # (onset time, peak level)
        self._impulses: list[float] = []
        self._last_loud = -10.0      # last non-impulse loud slice, for "quiet before"
        self._last_fire = -10.0
        self._ring_until = 0.0
        self._now = 0.0
        self._buffer = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> bool:
        self._buffer = np.concatenate([self._buffer, samples.astype(np.float32)])
        fired = False
        while len(self._buffer) >= SLICE:
            chunk, self._buffer = self._buffer[:SLICE], self._buffer[SLICE:]
            fired = self._slice(chunk) or fired
        return fired

    def _slice(self, chunk: np.ndarray) -> bool:
        self._now += SLICE / SAMPLE_RATE
        rms, brightness = _features(chunk)
        previous, self._previous = self._previous, rms

        if self._now < self._ring_until:
            # The room's echo of a clap that was just counted: neither a new
            # sound nor a reason to call the room noisy.
            pass
        elif self._pending is not None:
            onset, peak = self._pending
            peak = max(peak, rms)
            self._pending = (onset, peak)
            if rms < peak * DECAY:
                self._pending = None
                self._impulses.append(onset)
                self._ring_until = onset + RING
            elif self._now - onset > DECAY_WINDOW:
                # Too long to be a clap: a voice, music, a door. Quiet is broken.
                self._pending = None
                self._last_loud = self._now
        elif (rms >= MIN_LEVEL and rms >= self._background * OVER_BACKGROUND
              and rms >= previous * RISE and brightness >= MIN_BRIGHTNESS):
            self._pending = (self._now, rms)
        else:
            if rms >= max(MIN_LEVEL, self._background * OVER_BACKGROUND):
                self._last_loud = self._now
            # Background follows the room slowly, and only outside impulses.
            self._background += 0.02 * (rms - self._background)
            self._background = max(self._background, 1e-4)

        return self._check()

    def _check(self) -> bool:
        now = self._now
        self._impulses = [t for t in self._impulses if now - t <= MAX_GAP + QUIET_AROUND + 0.2]
        if len(self._impulses) < 2 or self._pending is not None:
            return False
        first, second = self._impulses[-2], self._impulses[-1]
        if now - second < QUIET_AROUND:
            return False                      # wait to be sure no third one follows
        if len(self._impulses) > 2 and first - self._impulses[-3] < QUIET_AROUND:
            self._impulses.clear()
            return False
        self._impulses.clear()
        if not MIN_GAP <= second - first <= MAX_GAP:
            return False
        if first - self._last_loud < QUIET_AROUND or self._last_loud > second:
            return False
        if now - self._last_fire < COOLDOWN:
            return False
        self._last_fire = now
        return True


class ClapDetector:
    """Runs DoubleClapTracker on its own thread; on_detect() is called from it."""

    def __init__(self, on_detect: Callable[[], None], logger: Callable[[str], None] = print):
        self._on_detect = on_detect
        self._logger = logger
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ClapThread")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    def feed(self, frame_int16) -> None:
        """Called from the mic callback: copy and queue, never block."""
        if not self._running:
            return
        try:
            data = frame_int16[:, 0] if getattr(frame_int16, "ndim", 1) > 1 else frame_int16
            self._queue.put_nowait(data.astype(np.float32) / 32768.0)
        except Exception:
            pass

    def _loop(self) -> None:
        tracker = DoubleClapTracker()
        while self._running:
            try:
                samples = self._queue.get()
                if samples is None or not self._running:
                    break
                if tracker.push(samples):
                    self._on_detect()
            except Exception as e:
                self._logger(f"Clap: detection error — {e}")
                time.sleep(0.1)
