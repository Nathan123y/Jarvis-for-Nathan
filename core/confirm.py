"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


_notify_cb: Optional[Callable[[str], None]] = None


def bind(show, hide, log=None, notify=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup.
    `notify(text)` tells the voice model how a confirmed action actually ended,
    so it never has to guess whether an email went out."""
    global _show_cb, _hide_cb, _log_cb, _notify_cb
    _show_cb, _hide_cb, _log_cb, _notify_cb = show, hide, log, notify


def _notify(outcome: str) -> None:
    if _notify_cb:
        try:
            _notify_cb(f"[ACTION_RESULT] {outcome} Tell the user in one short, natural "
                       f"sentence, exactly as it happened; do not call any tools.")
        except Exception:
            pass


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    global _pending

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic())

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] Waiting for the user's OK for: {title}. "
        f"In one short, natural sentence in their language, say what you're about to "
        f"do (who it goes to and the gist) and ask if you should go ahead. They answer "
        f"out loud (yes / no) or on screen. When they say yes, the app does it by "
        f"itself: do NOT call the tool again. Do not claim it is done until it is."
    )


def resolve(accepted: bool) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        _notify(f"The request timed out before it was confirmed, so it was NOT done: {p.title}.")
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        _notify(f"Cancelled; nothing was done: {p.title}.")
        return

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
            _notify(f"Confirmed; the action ran. Its result (report this, not your assumption): {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")
            _notify(f"It FAILED and was NOT done ({p.title}): {str(e)[:160]}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


_NO = ("no", "nope", "nah", "cancel", "dont", "don't", "stop", "wait", "hold",
       "never", "nevermind", "not", "abort", "scratch")
_YES_STRONG = {"yes", "yeah", "yea", "yep", "yup", "sure", "ok", "okay", "confirm",
               "confirmed", "send", "do", "go", "correct", "absolutely", "definitely",
               "alright", "affirmative", "proceed", "approved", "approve"}
_YES_FILLER = {"it", "ahead", "please", "thats", "that's", "right", "sir", "jarvis",
               "all", "good", "fine", "yes", "now", "then", "sounds", "and", "the",
               "message", "email", "text", "call", "for", "me", "on", "of", "course",
               "just", "go", "do"}
_MAX_WORDS = 7
# Words that mean "yes, but with a change" — never treated as a plain yes.
_QUALIFY = {"but", "change", "instead", "actually", "edit", "fix", "except", "first",
            "before", "add", "remove", "different", "rewrite", "make", "subject",
            "say", "saying", "tell", "wrong", "should", "could", "would", "what", "why",
            "how", "who", "when", "where", "which", "if", "or", "maybe", "later"}


def classify(text: str):
    """True for a plain spoken yes ("yes", "yeah send it", "go ahead"), False for a
    no/cancel/wait, None for anything else (ignored — the request stays open).

    Deliberately narrow: only a short utterance made of yes-words counts, so a new
    request or a question ("yes, but change the time") never sends anything."""
    import re
    words = re.sub(r"[^a-z' ]+", " ", str(text or "").lower()).split()
    if not words:
        return None
    if any(w in _NO for w in words):
        return False
    if len(words) > _MAX_WORDS:
        return None
    if any(w in _YES_STRONG for w in words) and all(w in _YES_STRONG or w in _YES_FILLER
                                                   for w in words):
        return True
    # "yes send it to her", "yeah go ahead and send that": a clear yes up front,
    # nothing that changes or questions it after.
    if words[0] in _YES_STRONG and not any(w in _QUALIFY for w in words) and len(words) <= _MAX_WORDS:
        return True
    return None


def voice_answer(text: str, started_at: float) -> Optional[bool]:
    """Resolve the pending confirmation from what the USER said (speech
    transcription, which the model cannot write). `started_at` is the monotonic
    time the utterance began; speech from before the request was made is ignored.
    Returns the answer acted on, or None if nothing was resolved."""
    with _lock:
        p = _pending
    if p is None or time.monotonic() - p.at > TIMEOUT_SECONDS or started_at < p.at:
        return None
    answer = classify(text)
    if answer is None:
        return None
    _log(f"SYS: Heard {'yes' if answer else 'no'} — {p.title}")
    resolve(answer)
    return answer


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title
