"""Audio streams that outlive a Live session.

The Live API ends a session every few minutes — every ~2.5 minutes while the
microphone is muted and nothing is being sent. Each rotation used to close the
microphone and speaker and open them again. Stopping a CoreAudio stream can
deadlock inside PortAudio (FinishStoppingStream waits on the HAL while the HAL's
IO thread waits on the audio unit), and the session teardown waited on that
close forever: Jarvis never reconnected, and after a long mute it simply
stopped hearing the user.

So a stream is opened once and handed to each new session. It is only closed
when the device or its settings change, and that close is never waited on for
longer than CLOSE_TIMEOUT.
"""

import asyncio

CLOSE_TIMEOUT = 3.0


async def close_stream(stream, timeout: float = CLOSE_TIMEOUT) -> bool:
    """Stop and close a sounddevice stream off the event loop.

    The close is shielded so a cancellation cannot leave the device half open,
    and bounded so a wedged driver cannot hold the caller hostage: after
    `timeout` the stream is abandoned to its worker thread. Returns whether it
    closed in time."""
    def _close():
        try:
            stream.stop()
        finally:
            stream.close()
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.to_thread(_close)), timeout)
        return True
    except asyncio.TimeoutError:
        print(f"[JARVIS] ⚠️  Audio stream did not close within {timeout:.0f}s — abandoned")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[JARVIS] ⚠️  Audio stream close: {e}")
    return False


class KeptStream:
    """One open audio stream, reused by every session that asks for the same
    device and settings (`key`)."""

    def __init__(self):
        self.stream = None
        self.key = None
        self.extra = None       # anything that must live exactly as long as the stream

    def reusable(self, key) -> bool:
        stream = self.stream
        return (stream is not None and self.key == key
                and bool(getattr(stream, "active", False)))

    def keep(self, stream, key, extra=None) -> None:
        self.stream, self.key, self.extra = stream, key, extra

    async def close(self) -> None:
        stream, extra = self.stream, self.extra
        self.stream = self.key = self.extra = None
        if extra is not None and hasattr(extra, "shutdown"):
            extra.shutdown(wait=False, cancel_futures=True)
        if stream is not None:
            await close_stream(stream)
