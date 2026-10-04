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


def parse_output_latency(value):
    """Turn the optional ``output_latency`` setting into what PortAudio accepts.

    ``None`` means "do not pass a latency", which keeps the library default
    (PortAudio's "low"). Anything else is a deliberate choice of speaker buffer:

    * ``"low"`` / ``"high"`` — PortAudio's own presets. "high" is a larger
      device buffer, so a late audio hand-off from Python is absorbed instead
      of becoming an audible gap, at the price of a slightly later first word.
    * a number of seconds between 0.02 and 1.0, as a number or a string.

    Anything unrecognised returns ``None`` rather than raising, so a typo in a
    settings file can never stop the speaker from opening.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("low", "high"):
            return text
        if text in ("", "default", "auto"):
            return None
        value = text
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0.02 <= seconds <= 1.0 else None
