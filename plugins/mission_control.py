"""JARVIS — Mission Control, version 1.0.

INSTALL: Put this file beside pomodoro.py in your plugins folder, then fully
restart JARVIS with your usual python3 main.py command. No extra packages.

TRY SAYING:
  "Add my EE97 lab report to Mission Control, due Friday, high priority."
  "Give me my mission briefing." / "What should I work on next?"
  "Mark mission 1 complete." / "Move mission 2 to tomorrow."
  "Show my completed missions." / "Undo my last mission change."

Uses the PLUGIN/run contract in _template.py and the show_content/write_log
hooks used by pomodoro.py. Saves only task data in memory/mission_control.json.
The plugin itself is offline; JARVIS's existing voice/model layer may still
need its normal connection. It does not read Canvas or send timed alerts.
Due dates are local calendar dates, not appointment times. No work starts
on import. Display failures do not prevent spoken results or saving.
"""

import copy
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parent.parent / "memory" / "mission_control.json"
_LOCK = threading.RLock()
_PRIORITIES = {"high": 0, "normal": 1, "low": 2}
_ACTIONS = ["add", "update", "complete", "reopen", "list", "briefing", "stats", "undo", "help"]
_LIMIT = 2000

PLUGIN = {
    "name": "mission_control",
    "description": (
        "A persistent Mission Control task board for assignments, projects and personal "
        "to-dos. Use for 'add a mission', 'track this assignment', 'what should I work "
        "on next', 'give me my mission briefing', 'show my tasks', 'mark this task done', "
        "'change its due date', or 'undo my last mission change'. For mission undo, "
        "call this tool with action=undo, not the separate core undo tool. These are tasks the "
        "USER asks to save; do not invent tasks or deadlines. It does NOT read Canvas, "
        "email or calendars and does NOT send scheduled alerts. Use pomodoro for focus "
        "timers and reminder for timed notifications. For changes, use the task ID "
        "returned by this tool; call list first if unknown. If a title matches multiple "
        "tasks, ask which ID. Use 'due' only for user-stated deadlines. Pass today or "
        "tomorrow literally; bare weekdays mean the next occurrence including today. "
        "For ambiguous dates ask the user; otherwise use YYYY-MM-DD. Omit unchanged "
        "fields on update. Briefings rank overdue tasks first, then today's tasks, then "
        "upcoming deadlines, then undated tasks; priority breaks ties on the same day. "
        "Speak the result in the user's language; never claim a change if this tool failed."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": _ACTIONS,
                       "description": "Default briefing. undo reverses the last saved change."},
            "task_id": {"type": "INTEGER", "description": "Stable task ID for update, complete or reopen."},
            "title": {"type": "STRING", "description": "Task title for add, or new title on update."},
            "project": {"type": "STRING", "description": "Course/project on add/update; exact project filter for list/briefing/stats. Empty clears it on update."},
            "due": {"type": "STRING", "description": "YYYY-MM-DD, today, tomorrow, a weekday, or 'in N days'. Empty or 'none' clears on update. No time of day."},
            "priority": {"type": "STRING", "enum": ["high", "normal", "low"], "description": "Default normal when adding."},
            "minutes": {"type": "INTEGER", "description": "User's estimated work minutes, 0-1440. Zero means unknown."},
            "notes": {"type": "STRING", "description": "Optional task notes; empty clears on update."},
            "status": {"type": "STRING", "enum": ["open", "done", "all"], "description": "list only. Default open."},
        },
        "required": [],
    },
}


def _text(value, field, limit):
    if not isinstance(value, str):
        raise ValueError(field + " must be text.")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValueError("Please shorten " + field + " to " + str(limit) + " characters.")
    return value


def _integer(value, field, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not low <= value <= high or int(value) != value):
        raise ValueError(f"{field} must be a whole number from {low} to {high}.")
    return int(value)


def _due(value):
    value = _text(value, "due", 40).lower()
    today = date.today()
    if value in ("", "none", "no deadline", "no due date"):
        return ""
    if value in ("today", "tomorrow"):
        return (today + timedelta(days=value == "tomorrow")).isoformat()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if value in weekdays:
        return (today + timedelta(days=(weekdays.index(value) - today.weekday()) % 7)).isoformat()
    match = re.fullmatch(r"in (\d{1,3}) days?", value)
    if match:
        return (today + timedelta(days=int(match.group(1)))).isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a due date like 2026-10-09, today, tomorrow, Friday, or in 3 days.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("That calendar date is invalid. Please check the due date.") from None


def _validate_tasks(tasks):
    if not isinstance(tasks, list) or len(tasks) > _LIMIT:
        raise ValueError("Invalid task list")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Invalid task")
        task_id = _integer(task.get("id"), "id", 1, 2**53 - 1)
        if task_id in seen:
            raise ValueError("Duplicate IDs")
        seen.add(task_id)
        for field, limit in (("title", 180), ("project", 80), ("notes", 2000)):
            _text(task.get(field), field, limit)
        if not task["title"].strip() or task.get("priority") not in _PRIORITIES:
            raise ValueError("Invalid task fields")
        if task.get("status") not in ("open", "done"):
            raise ValueError("Invalid status")
        _integer(task.get("minutes"), "minutes", 0, 1440)
        for field in ("due", "completed"):
            val = task.get(field)
            if not isinstance(val, str) or (val and date.fromisoformat(val).isoformat() != val):
                raise ValueError("Invalid stored date")


def _load():
    if not STATE_FILE.exists():
        return {"version": 1, "next_id": 1, "tasks": [], "undo": None}
    try:
        if STATE_FILE.stat().st_size > 20_000_000:
            raise ValueError("Oversized state")
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unknown format")
        _validate_tasks(state["tasks"])
        next_id = _integer(state["next_id"], "next_id", 1, 2**53 - 1)
        groups = [state["tasks"]]
        if state.get("undo") is not None:
            _validate_tasks(state["undo"])
            groups.append(state["undo"])
        if any(task["id"] >= next_id for group in groups for task in group):
            raise ValueError("Invalid next ID")
        return state
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("The Mission Control save file is damaged or has an unsupported format. "
                         "I left it unchanged; restore memory/mission_control.json from a backup.") from None


@contextmanager
def _transaction():
    """Serializes threads; also serializes processes on macOS/Linux via flock."""
    with _LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.with_suffix(".lock").open("a+b") as lockfile:
            try:
                import fcntl
            except ImportError:  # Windows: use one JARVIS process.
                fcntl = None
            if fcntl:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def _save(state):
    """Atomic replacement: failed writes leave the previous save intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE_FILE.parent,
                                         prefix=".mission_", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _sort(task):
    return (task["due"] or "9999-99-99", _PRIORITIES[task["priority"]], task["id"])


def _when(task):
    if not task["due"]:
        return "no deadline"
    days = (date.fromisoformat(task["due"]) - date.today()).days
    if days < 0:
        return f"{abs(days)} day(s) overdue"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return "due " + task["due"]


def _line(task):
    timing = ("completed " + task["completed"]) if task["status"] == "done" else _when(task)
    detail = timing + " | " + task["priority"]
    if task["project"]:
        detail += " | " + task["project"]
    if task["minutes"]:
        detail += f" | ~{task['minutes']} min"
    return f"#{task['id']}  {task['title']}\n   {detail}"


def _board(tasks):
    active = sorted((t for t in tasks if t["status"] == "open"), key=_sort)
    done = sum(t["status"] == "done" for t in tasks)
    overdue = sum(bool(t["due"]) and t["due"] < date.today().isoformat() for t in active)
    lines = ["MISSION CONTROL  /  " + date.today().isoformat(),
             f"{len(active)} open  |  {done} completed  |  {overdue} overdue", ""]
    if active:
        lines += ["NEXT UP", _line(active[0]), "", "OPEN MISSIONS"]
        for task in active[:30]:
            lines += [_line(task)]
            if task["notes"]:
                lines += ["   Note: " + task["notes"][:220]]
            lines.append("")
        if len(active) > 30:
            lines.append(f"Showing 30 of {len(active)}. Ask for a specific project to narrow the board.")
    else:
        lines.append("All clear. Add a mission whenever you're ready.")
    return "\n".join(lines)


def _fields(parameters, task):
    for field, limit in (("title", 180), ("project", 80), ("notes", 2000)):
        if field in parameters:
            task[field] = _text(parameters[field], field, limit)
    if not task["title"]:
        raise ValueError("Tell me the task title first.")
    if "due" in parameters:
        task["due"] = _due(parameters["due"])
    if "priority" in parameters:
        priority = _text(parameters["priority"], "priority", 10).lower()
        if priority not in _PRIORITIES:
            raise ValueError("Priority must be high, normal, or low.")
        task["priority"] = priority
    if "minutes" in parameters:
        task["minutes"] = _integer(parameters["minutes"], "minutes", 0, 1440)


def _operate(parameters):
    action = _text(parameters.get("action", "briefing"), "action", 20).lower()
    if action not in _ACTIONS:
        raise ValueError("Choose add, update, complete, reopen, list, briefing, stats, undo, or help.")
    if action == "help":
        help_text = ("Try: add a mission, give me my mission briefing, mark mission 1 complete, "
                     "move mission 2 to tomorrow, show completed missions, or undo my last mission change. "
                     "Tasks persist across restarts. This board does not send timed alerts or sync Canvas.")
        return help_text, help_text
    with _transaction():
        state = _load()
        tasks = state["tasks"]
        before = copy.deepcopy(tasks)
        changed = False
        if action == "add":
            if len(tasks) >= _LIMIT:
                raise ValueError("The board has reached its 2,000-task limit. Back it up before starting a new board.")
            task = {"id": state["next_id"], "title": "", "project": "", "due": "",
                    "priority": "normal", "minutes": 0, "notes": "", "status": "open", "completed": ""}
            _fields(parameters, task)
            duplicate = next((t for t in tasks if t["status"] == "open" and
                              all(str(t[k]).casefold() == str(task[k]).casefold()
                                  for k in ("title", "project", "due"))), None)
            if duplicate:
                return (f"That open mission already exists as #{duplicate['id']}. "
                        "Use update to change its details.", _board(tasks))
            tasks.append(task)
            state["next_id"] += 1
            message = f"Mission #{task['id']} saved: {task['title']}, {_when(task)}, {task['priority']} priority."
            changed = True
        elif action in ("update", "complete", "reopen"):
            task_id = _integer(parameters.get("task_id"), "task_id", 1, 2**53 - 1)
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task is None:
                raise ValueError(f"I couldn't find mission #{task_id}. Ask to show your missions.")
            if action == "update":
                _fields(parameters, task)
                message = f"Updated mission #{task_id}: {task['title']}, {_when(task)}, {task['priority']} priority."
            else:
                desired = "done" if action == "complete" else "open"
                if task["status"] == desired:
                    return f"Mission #{task_id} is already {desired}.", _board(tasks)
                task["status"] = desired
                task["completed"] = date.today().isoformat() if desired == "done" else ""
                message = f"Mission #{task_id} {'completed' if desired == 'done' else 'reopened'}: {task['title']}."
            changed = tasks != before
            if not changed:
                message = f"Mission #{task_id} already has those details. No changes made."
        elif action == "undo":
            if state.get("undo") is None:
                return "There is no saved mission change to undo.", _board(tasks)
            state["tasks"] = tasks = state["undo"]
            state["undo"] = None
            # Never reuse IDs, even after undoing an addition.
            _save(state)
            return "Undid the last mission change. Your board is updated.", _board(tasks)
        else:
            project = _text(parameters.get("project", ""), "project", 80)
            if project:
                tasks = [t for t in tasks if t["project"].casefold() == project.casefold()]
            active = sorted((t for t in tasks if t["status"] == "open"), key=_sort)
            today = date.today().isoformat()
            done_today = sum(t["status"] == "done" and t["completed"] == today for t in tasks)
            overdue = sum(bool(t["due"]) and t["due"] < today for t in active)
            due_today = sum(t["due"] == today for t in active)
            scope = f" In {project}," if project else ""
            if action == "stats":
                known = [t["minutes"] for t in active if t["minutes"]]
                message = (f"{scope} You have {len(active)} open missions, {overdue} overdue, "
                           f"and {due_today} due today. You completed {done_today} today. "
                           f"Estimated remaining work: {sum(known)} minutes across {len(known)} "
                           f"tasks with estimates; {len(active) - len(known)} have no estimate.").strip()
                return message, _board(tasks) + "\n\n" + message
            if action == "list":
                status = _text(parameters.get("status", "open"), "status", 10).lower()
                if status not in ("open", "done", "all"):
                    raise ValueError("Status must be open, done, or all.")
                selected = sorted((t for t in tasks if status == "all" or t["status"] == status), key=_sort)
                panel = "\n\n".join(_line(t) for t in selected[:30]) or "No matching missions."
                if len(selected) > 30:
                    panel += f"\n\nShowing 30 of {len(selected)}; filter by project to narrow the list."
                message = f"{len(selected)} matching missions."
                if selected:
                    message += " " + "; ".join(f"#{t['id']}: {t['title']}" for t in selected[:3]) + "."
                return message, panel
            message = f"{scope} {len(active)} open missions, {overdue} overdue, {due_today} due today.".strip()
            if active:
                task = active[0]
                message += f" I suggest #{task['id']}: {task['title']}, {_when(task)}."
                if task["minutes"]:
                    message += f" You estimated {task['minutes']} minutes."
            else:
                message += " Your board is clear."
            return message, _board(tasks)
        if changed:
            state["undo"] = before
            _save(state)
        return message, _board(tasks)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """JARVIS entry point. Always return a spoken result; never raise."""
    try:
        if not isinstance(parameters, dict):
            raise ValueError("Mission Control needs a dictionary of parameters.")
        message, panel = _operate(parameters)
        if player is not None:
            try:
                player.show_content("MISSION CONTROL", panel)
            except Exception:
                pass
            try:
                player.write_log("JARVIS: " + message)
            except Exception:
                pass
        return message
    except ValueError as error:
        return "Mission Control: " + str(error)
    except OSError:
        return ("Mission Control couldn't access or save its local task file. "
                "Check that JARVIS can write to its memory folder and that disk space is available. "
                "No task change was saved.")
    except Exception:
        return "Mission Control couldn't finish that request. Ask to show your missions before trying the change again."
