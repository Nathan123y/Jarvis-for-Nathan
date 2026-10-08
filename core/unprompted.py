"""When Jarvis is allowed to start talking on its own.

The background speakers (system monitor, news monitor, proactive remarks, the
"while you were away" briefing) all decide this the same way, so the rule lives
here where it can be tested.

Muted is the important one. Jarvis cannot hear the microphone while it is
speaking, so a remark made into a muted mic is not just unheard: it is also why
Jarvis looks dead when the user comes back, unmutes and starts talking into the
middle of a monologue it cannot interrupt. Muted means "I am not in this
conversation", so nothing starts by itself.
"""
from __future__ import annotations


def may_speak(*, connected: bool, awake: bool, muted: bool, speaking: bool) -> bool:
    """True only when an unprompted remark can actually reach the user."""
    return bool(connected) and bool(awake) and not muted and not speaking


def silence_restarts(was_muted: bool, now_muted: bool) -> bool:
    """True when the mute switch has just moved: the user arrived or stepped away, so the
    "how long have they been silent" clock starts again and a proactive remark does not
    fire the instant they come back."""
    return bool(was_muted) != bool(now_muted)
