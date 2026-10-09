"""Noticing that the live connection has died without saying so.

The microphone path has no way to complain. `_enqueue_audio` drops the oldest
block when the sender falls behind, and a send into a half-open socket can sit
there without raising: the operating system accepts the bytes and nobody ever
answers. Jarvis then looks perfectly healthy -- the HUD says LISTENING, the mic
light is on, frames are being handed over -- and every word the user says is
dropped on the floor.

Nothing else detects this. A rotation is announced by the server (GoAway), an
error unwinds the task group, but a connection that simply stops talking
produces neither. So the one thing we can rely on is that a working session is
never silent for long: the server sends resumption handles and turn events on
its own. If we are streaming audio and have heard nothing back for minutes, the
session is gone and the only cure is to rebuild it.
"""
from __future__ import annotations

# A healthy session sends something (a resumption handle at the very least) well
# inside this window. Long, because a needless reconnect costs the user a pause.
QUIET_LIMIT_S = 180.0
# Only judge a session we are actually feeding: muted, asleep and push-to-talk
# sessions are quiet on both sides, and that is not a fault.
MIC_RECENT_S = 60.0


def should_reconnect(*, now: float, last_server_msg: float, last_mic_send: float,
                     quiet_limit: float = QUIET_LIMIT_S, mic_recent: float = MIC_RECENT_S) -> bool:
    """True when we are sending audio into a connection that has stopped answering."""
    if not last_server_msg or not last_mic_send:
        return False                         # nothing to compare yet
    if now - last_mic_send > mic_recent:
        return False                         # not feeding it: silence proves nothing
    return (now - last_server_msg) > quiet_limit
