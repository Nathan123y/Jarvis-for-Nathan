#!/usr/bin/env python3
"""Claude Code hook (SessionStart + UserPromptSubmit): the Jarvis Brain, brought to mind.

SessionStart       a short brief: the last nightly summary, last week, what is still
                   open, today's activity, recent lessons, and how to search the brain.
                   Also re-indexes Claude's memory notes that changed.
UserPromptSubmit   memories clearly related to what was just typed (nothing when
                   nothing is clearly related), like something coming back to mind.

Both come from the local index (core/brain_index.py), so they are fast and work offline.
Prints the hook's JSON on stdout; never fails a session (always exits 0).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def context_for(hook: dict) -> tuple[str, str]:
    from core import brain_index, brain_recall, vault_brain
    event = hook.get("hook_event_name") or ""
    if event == "SessionStart":
        brain = vault_brain.brain_dir(create=False)
        if brain is not None:
            brain_index.refresh(brain, with_logs=False, with_vault_notes=False)
        return event, brain_recall.session_brief()
    if event == "UserPromptSubmit":
        return event, brain_recall.prompt_context(str(hook.get("prompt") or ""))
    return event, ""


def main() -> int:
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        event, text = context_for(hook)
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
    except Exception as e:
        print(f"claude_brain_context: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
