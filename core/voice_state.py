"""Resolve overlapping speaker/tool events without changing audio timing."""


def runtime_state(requested, *, awake, speaking, pending_tools=0):
    if requested in {"LISTENING", "THINKING"}:
        if not awake:
            return "SLEEPING"
        if speaking:
            return "SPEAKING"
        if pending_tools:
            return "THINKING"
    return requested


def can_auto_sleep(*, speaking, pending_tools, queued_audio):
    return not (speaking or pending_tools or queued_audio)
