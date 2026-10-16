"""Routines: one phrase, several things done at once.

Built in:
- study mode: do-not-disturb on (texts, deliveries and group chats are held,
  with a digest at the end), a pomodoro, Canvas opened, focus music playing.
  "End study mode" undoes it all.
- goodnight: tomorrow's deadlines, music paused, do-not-disturb until the
  morning, a wake-up reminder, and the Mac locked once Jarvis has finished
  speaking.
Custom ones: "create a routine called gym: play my gym playlist and start a
45 minute timer" saves the steps; saying the routine's name runs them.
Settings (study playlist, timer length, wake time) live in the config.
"""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from memory.config_manager import _save_flag, get_plugin_config, get_plugin_enabled, load_api_keys

PLUGIN = {
    "name": "routines",
    "description": (
        "Run multi-step routines from one phrase. Use for 'study mode' / 'time to study' / "
        "'start studying' (action study), 'end study mode' / 'I'm done studying' (action "
        "end_study), 'goodnight' / 'I'm going to bed' (action goodnight, optional wake_time "
        "HH:MM), and the user's own routines: 'create a routine called X that does A, B, C' "
        "(action create, name + steps as a list of short instructions), 'run my X routine' "
        "or just the routine's name (action run), list, delete, and settings for the built-in "
        "ones (action configure: study_playlist, study_minutes, wake_time). After a custom "
        "routine is run, do every step it returns, in order, with your tools."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["study", "end_study", "goodnight", "run", "create",
                                                  "list", "delete", "configure"]},
            "name": {"type": "STRING", "description": "Custom routine name"},
            "steps": {"type": "ARRAY", "items": {"type": "STRING"},
                      "description": "For create: each step as a short instruction"},
            "task": {"type": "STRING", "description": "For study: what the user is studying"},
            "wake_time": {"type": "STRING", "description": "HH:MM, 24h"},
            "study_playlist": {"type": "STRING"},
            "study_minutes": {"type": "INTEGER"},
        },
        "required": ["action"],
    },
}

_DEFAULTS = {"study_playlist": "lofi focus study", "study_minutes": 25, "wake_time": "07:30"}
_LOCK_DELAY = 14.0            # seconds: let Jarvis finish saying goodnight first


def _settings() -> dict:
    raw = load_api_keys().get("routine_settings") or {}
    return {**_DEFAULTS, **(raw if isinstance(raw, dict) else {})}


def _custom() -> dict:
    raw = load_api_keys().get("routines") or {}
    return raw if isinstance(raw, dict) else {}


def _key(name) -> str:
    return " ".join(str(name or "").lower().replace("routine", "").split())


def _step(label: str, fn, done: list, failed: list) -> None:
    try:
        out = fn()
        if out is not False:
            done.append(label)
            return
    except Exception as exc:
        print(f"[Routines] {label} failed: {exc!r}")
    failed.append(label)


def _canvas_url() -> str:
    cfg = get_plugin_config("canvas")
    for value in (cfg.get("domain"), cfg.get("calendar_feed")):
        host = urlsplit(str(value or "") if "://" in str(value or "") else f"https://{value}").hostname
        if host and host.endswith(".instructure.com"):
            return f"https://{host}/"
    return ""


def _music(action: str, **kw):
    if not get_plugin_enabled("youtube_music"):
        return False
    from plugins import youtube_music
    out = youtube_music.run({"action": action, **kw})
    return not any(w in out.lower() for w in ("couldn't", "didn't", "needs"))


# ── built-ins ────────────────────────────────────────────────────────────────
def study(player, task: str = "") -> str:
    from core import dnd
    s = _settings()
    done, failed = [], []
    _step("messages held", lambda: dnd.turn_on("study mode"), done, failed)
    if get_plugin_enabled("pomodoro"):
        from plugins import pomodoro
        _step(f"{s['study_minutes']} minute focus timer",
              lambda: pomodoro.run({"action": "start", "work_minutes": int(s["study_minutes"]),
                                    **({"task": task} if task else {})}, player=player), done, failed)
    url = _canvas_url()
    if url:
        _step("Canvas open", lambda: subprocess.run(["open", url], timeout=10).returncode == 0, done, failed)
    _step("focus music", lambda: _music("play", query=s["study_playlist"], kind="playlist"), done, failed)
    return ("Study mode on: " + ", ".join(done) + "." +
            (f" Couldn't do: {', '.join(failed)}." if failed else "") +
            " Texts, deliveries and group chats will wait until you say you're done studying.")


def end_study(player) -> str:
    from core import dnd
    was = dnd.reason()
    dnd.turn_off()
    if get_plugin_enabled("pomodoro"):
        try:
            from plugins import pomodoro
            pomodoro.run({"action": "stop"}, player=player)
        except Exception:
            pass
    _music("pause")
    return ("Study mode off. Anything that came in will be told in a moment." if was
            else "Study mode wasn't on, but I've stopped the timer and music.")


def _next_wake(wake: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    hh, mm = (int(x) for x in wake.split(":"))
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return t if t > now + timedelta(minutes=30) else t + timedelta(days=1)


def _tomorrow(day: date) -> str:
    """Tomorrow's deadlines from Mission Control and Canvas, as one sentence."""
    from plugins import daily_briefing as brief
    want = day.isoformat()
    parts = []
    try:
        tasks, _ = brief._missions()
        due = [t["title"] for t in tasks if t.get("due") and t["due"] <= want]
        if due:
            parts.append("missions due: " + "; ".join(due[:4]))
    except Exception:
        pass
    try:
        items, _ = brief._canvas()
        school = [c["name"].split(" [")[0] for c in items if c.get("due") == want or c.get("overdue")]
        if school:
            parts.append("school due: " + "; ".join(school[:3]))
    except Exception:
        pass
    return ("Tomorrow, " + ". ".join(parts) + ".") if parts else "Nothing is due tomorrow."


def _lock_mac() -> None:
    time.sleep(_LOCK_DELAY)
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "q" '
                        'using {control down, command down}'], capture_output=True, timeout=10)
    if r.returncode != 0:
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True, timeout=10)


def goodnight(player, wake_time: str = "") -> str:
    from core import dnd
    wake = wake_time or _settings()["wake_time"]
    try:
        when = _next_wake(wake)
    except Exception:
        when = _next_wake(_DEFAULTS["wake_time"])
    preview = _tomorrow(when.date())
    _music("pause")
    dnd.turn_on("the night", until=when.timestamp())
    alarm = ""
    try:
        from actions.reminder import reminder
        out = reminder({"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                        "message": "Good morning! Time to get up."})
        if out.startswith("Reminder set"):
            alarm = f" Wake-up reminder at {when.strftime('%I:%M %p').lstrip('0')}."
    except Exception as exc:
        print(f"[Routines] wake reminder failed: {exc!r}")
    threading.Thread(target=_lock_mac, daemon=True, name="goodnight-lock").start()
    return (f"{preview}{alarm} Messages are held until morning, and I'll lock the Mac in a few "
            "seconds. Say goodnight to the user warmly in one short sentence.")


# ── custom ───────────────────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "run").strip().lower()
    try:
        if action == "study":
            return study(player, str(args.get("task") or ""))
        if action == "end_study":
            return end_study(player)
        if action == "goodnight":
            return goodnight(player, str(args.get("wake_time") or ""))
        if action == "configure":
            s = {k: v for k, v in _settings().items()}
            changed = []
            for k in ("study_playlist", "study_minutes", "wake_time"):
                if args.get(k) not in (None, ""):
                    if k == "wake_time":
                        _next_wake(str(args[k]))           # validates HH:MM
                    s[k] = int(args[k]) if k == "study_minutes" else str(args[k])
                    changed.append(f"{k.replace('_', ' ')} {s[k]}")
            _save_flag("routine_settings", s)
            return ("Saved: " + ", ".join(changed) + ".") if changed else \
                f"Study playlist {s['study_playlist']}, {s['study_minutes']} minute timer, wake at {s['wake_time']}."
        routines = _custom()
        name = _key(args.get("name"))
        if action == "create":
            steps = [" ".join(str(x).split())[:200] for x in (args.get("steps") or []) if str(x).strip()]
            if not name or not steps:
                return "Tell me the routine's name and what it should do."
            routines[name] = steps[:12]
            _save_flag("routines", routines)
            return f"Saved the {name} routine: " + "; ".join(steps) + f". Say '{name}' to run it."
        if action == "list":
            names = list(routines)
            return ("Your routines: study mode, goodnight" + (", " + ", ".join(names) if names else "") + ".")
        if action == "delete":
            if routines.pop(name, None) is None:
                return f"There's no routine called {name}."
            _save_flag("routines", routines)
            return f"Deleted the {name} routine."
        if action == "run":
            if name in ("study", "study mode"):
                return study(player)
            if name in ("goodnight", "good night", "bedtime"):
                return goodnight(player)
            steps = routines.get(name)
            if not steps:
                close = [n for n in routines if name and (name in n or n in name)]
                steps = routines.get(close[0]) if len(close) == 1 else None
            if not steps:
                return f"There's no routine called {name}. Yours: study mode, goodnight" + \
                    ("".join(", " + n for n in routines)) + "."
            return ("Run this routine now: do each step in order with your tools, without asking "
                    "for confirmation unless a step sends a message or spends money, then confirm "
                    "in one short sentence.\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        return "Actions: study, end_study, goodnight, run, create, list, delete, configure."
    except Exception as exc:
        return f"The routine had a problem ({type(exc).__name__})."
