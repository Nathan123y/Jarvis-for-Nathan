"""Read-only Hevy workout history and exercise progress for Jarvis.

The user's Hevy Pro API key lives only in ignored local Plugin Settings.
Workout details are fetched on demand. No workout is created or changed.
"""
from __future__ import annotations

from datetime import datetime
from math import isfinite

from memory.config_manager import get_plugin_config


PLUGIN = {
    "name": "hevy_workouts",
    "description": (
        "Read the user's Hevy workouts and exercise history. Use when asked 'how was my "
        "last workout', 'show my Hevy workouts', 'how is my bench press progressing', "
        "'compare my last two leg days', or 'connect Hevy'. For questions about a "
        "specific exercise, use action exercise and pass its name. This is read-only; "
        "do not claim access to health data Hevy does not supply. Never ask the user "
        "to say or paste their API key in chat; it belongs in local Plugin Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "recent (default), workout (latest detailed workout), exercise (compare named exercise across sessions), or connect"},
            "exercise": {"type": "STRING", "description": "Exercise name or part of name for action exercise"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "hevy",
    "title": "Hevy workout history (read-only)",
    "fields": [{"key": "api_key", "type": "password", "label": "Hevy Pro API key",
                "placeholder": "Paste from hevy.com/settings?developer on your Mac"}],
}


def _clean(value, limit=100):
    return " ".join(str(value or "").split())[:limit]


def _number(value):
    try:
        n = float(value)
        return n if isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        return "Unknown date"


def _fetch(api_key, *, max_pages=1):
    """Fetch up to 10 workouts per page; do not redirect an API credential."""
    import requests

    found = []
    for page in range(1, max_pages + 1):
        response = requests.get(
            "https://api.hevyapp.com/v1/workouts",
            headers={"api-key": api_key, "Accept": "application/json"},
            params={"page": page, "pageSize": 10}, timeout=(4, 9), allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise ValueError("Hevy denied the key. Check your Hevy Pro API key in Plugin Settings.")
        if response.status_code != 200:
            raise ValueError("Hevy could not return workouts right now. Try again later.")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("workouts"), list):
            raise ValueError("Hevy returned an unexpected workout format.")
        found.extend(w for w in data["workouts"] if isinstance(w, dict))
        try:
            if page >= int(data.get("page_count", page)) or not data["workouts"]:
                break
        except (ValueError, TypeError, OverflowError):
            break
    found.sort(key=lambda w: str(w.get("start_time") or ""), reverse=True)
    return found


def _working_sets(exercise):
    return [s for s in (exercise.get("sets") or [])
            if isinstance(s, dict) and s.get("type", "normal") != "warmup"]


def _set_text(item):
    weight, reps = _number(item.get("weight_kg")), _number(item.get("reps"))
    if weight is not None and reps is not None:
        label = f"{weight:g} kg × {reps:g}"
    elif reps is not None:
        label = f"{reps:g} reps"
    elif (duration := _number(item.get("duration_seconds"))) is not None:
        label = f"{duration:g} seconds"
    elif (distance := _number(item.get("distance_meters"))) is not None:
        label = f"{distance:g} meters"
    else:
        label = "set logged"
    if (rpe := _number(item.get("rpe"))) is not None:
        label += f" @ RPE {rpe:g}"
    return label


def _exercises(workout):
    return [e for e in (workout.get("exercises") or []) if isinstance(e, dict)]


def _workout_lines(workout):
    lines = [f"{_date(workout.get('start_time'))} · {_clean(workout.get('title') or 'Workout')}"]
    for exercise in _exercises(workout)[:15]:
        sets = _working_sets(exercise)
        line = _clean(exercise.get("title") or "Exercise", 65)
        if sets:
            line += ": " + ", ".join(_set_text(s) for s in sets[:6])
        lines.append(line[:220])
        if exercise.get("notes"):
            lines.append("  Note: " + _clean(exercise["notes"], 120))
    if workout.get("description"):
        lines.append("Workout note: " + _clean(workout["description"], 160))
    return lines


def _exercise_history(workouts, query):
    matches = []
    for workout in workouts:
        for exercise in _exercises(workout):
            title = _clean(exercise.get("title"), 70)
            if query.casefold() not in title.casefold():
                continue
            sets = _working_sets(exercise)
            matches.append((_date(workout.get("start_time")), title, sets))
    return matches


def _best_set(sets):
    entries = [(float(s["weight_kg"]), float(s["reps"])) for s in sets
               if _number(s.get("weight_kg")) is not None
               and _number(s.get("reps")) is not None and float(s["reps"]) > 0]
    return max(entries, key=lambda pair: (pair[0], pair[1])) if entries else None


def _workout_feedback(latest, history):
    """Compare logged working sets with the previous matching exercise only."""
    comparisons = []
    for exercise in _exercises(latest):
        title = _clean(exercise.get("title"), 65)
        if not title:
            continue
        current = _best_set(_working_sets(exercise))
        if current is None:
            continue
        previous = next((old for workout in history for old in _exercises(workout)
                         if _clean(old.get("title"), 65).casefold() == title.casefold()), None)
        if previous is None:
            continue
        older = _best_set(_working_sets(previous))
        if older is None:
            continue
        comparisons.append(f"{title}: {current[0]:g} kg × {current[1]:g} reps "
                           f"(previous: {older[0]:g} kg × {older[1]:g} reps)")
    return comparisons


def _display(player, title, lines):
    if player:
        try:
            player.show_content(title, "\n".join(lines)[:3500])
        except Exception:
            pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "recent").strip().lower()
    if action == "connect":
        return ("Hevy's separate public API currently requires Hevy Pro. Get your key "
                "from hevy.com/settings?developer, then paste it under Jarvis Settings > "
                "Plugin Settings > Hevy and save. Keep the key private. Jarvis reads workouts "
                "on demand; your ChatGPT Hevy connection does not sign Jarvis in.")
    if action not in ("recent", "workout", "exercise"):
        return "Hevy commands: recent, workout, exercise, or connect."
    query = _clean(params.get("exercise"), 80)
    if action == "exercise" and len(query) < 2:
        return "Which Hevy exercise should I look up? Tell me its name."
    api_key = str(get_plugin_config("hevy").get("api_key") or "").strip()
    if not api_key:
        return "Hevy isn't connected to Jarvis yet. Say 'connect Hevy' for setup instructions."
    try:
        workouts = _fetch(api_key, max_pages=8 if action == "exercise" else 4 if action == "workout" else 2)
    except ValueError as exc:
        return str(exc)
    except Exception:
        return "Hevy couldn't be reached. Check your internet connection and try again."
    if not workouts:
        return "Hevy is connected, but no workouts were returned."

    if action == "workout":
        latest = workouts[0]
        lines = ["LATEST HEVY WORKOUT", *_workout_lines(latest)]
        comparisons = _workout_feedback(latest, workouts[1:])
        if comparisons:
            lines += ["", "VERSUS PREVIOUS MATCHING EXERCISES", *comparisons[:8]]
        _display(player, "HEVY WORKOUT", lines)
        return (f"Your latest Hevy workout was {_clean(latest.get('title') or 'a workout', 65)} "
                f"on {_date(latest.get('start_time'))}, with {len(_exercises(latest))} exercises. "
                + (f"Compared with the last matching session, {comparisons[0]}. "
                   if comparisons else "I don't have a prior matching set to compare yet. ")
                + "I've put the recorded sets and comparisons on screen. Consider your effort and form too.")

    if action == "exercise":
        matches = _exercise_history(workouts, query)
        if not matches:
            return (f"I didn't find '{query}' in the last {len(workouts)} Hevy workouts. "
                    "Try a shorter exercise name.")
        lines = [f"HEVY EXERCISE · {query}"]
        for when, name, sets in matches[:10]:
            lines.append(f"{when} · {name}: " + (", ".join(_set_text(s) for s in sets[:6]) or "no working sets"))
        _display(player, "HEVY PROGRESS", lines)
        comparison = ""
        if len(matches) > 1:
            current, previous = _best_set(matches[0][2]), _best_set(matches[1][2])
            if current and previous:
                comparison = (f" Your top logged set was {current[0]:g} kg × {current[1]:g} reps "
                              f"versus {previous[0]:g} kg × {previous[1]:g} reps the time before. "
                              "Weight and reps can change for different reasons; check effort and form too.")
        return (f"I found {len(matches)} sessions for {matches[0][1]} in the last "
                f"{len(workouts)} workouts.{comparison} The session details are on screen.")

    lines = ["RECENT HEVY WORKOUTS"]
    for workout in workouts[:8]:
        lines.append(f"{_date(workout.get('start_time'))} · {_clean(workout.get('title') or 'Workout')} "
                     f"· {len(_exercises(workout))} exercises")
    _display(player, "HEVY RECENT WORKOUTS", lines)
    return (f"I found {len(workouts)} recent Hevy workouts. The latest was "
            f"{_clean(workouts[0].get('title') or 'a workout', 65)} on "
            f"{_date(workouts[0].get('start_time'))}. I've put the recent list on screen.")
