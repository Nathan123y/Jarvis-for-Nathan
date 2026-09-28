"""Decide when a streaming speaker has actually drained between packets."""

import time


def playback_idle(last_write_at: float, scheduled_end: float, idle_for: float) -> bool:
    """A network gap alone does not mean the speaker has run out of audio.

    ``last_write_at`` uses the monotonic clock; ``scheduled_end`` is the wall
    clock estimate of when the last queued sample reaches the speaker.
    """
    return bool(last_write_at) and (
        time.monotonic() - last_write_at >= idle_for
        and time.time() >= scheduled_end
    )
