"""What Jarvis reads out when it opens: the mission log and what is due today.

Gathers, in parallel and with a short time limit so startup never waits long:
open Mission Control tasks (local file) and Canvas work that is overdue or due
today. Anything that is slow, disabled or unavailable is simply left out.
The Mac Calendar is not read here: asking it would launch the Calendar app on
every startup. ("Give me my daily briefing" still includes it.)

Everything returned is the user's own saved data. It is handed to the voice
model as data to read out, never as instructions.
"""
from __future__ import annotations

import threading
import time
from datetime import date

SPOKEN_ITEMS = 5          # how many missions to name out loud
PANEL_ITEMS = 12
GATHER_SECONDS = 6.0


def _clean(value, limit=90) -> str:
    return " ".join(str(value or "").split())[:limit]


def compose(tasks, events, canvas_items, today: str) -> tuple[str, str]:
    """(facts to speak, text for the on-screen panel). Pure: no I/O.

    tasks: open Mission Control tasks (dicts with title, due, priority, project), or
        None if Mission Control was disabled, unreadable or too slow (then it is
        not mentioned rather than reported as empty).
    events: [(clock, title)] for today, or None if the calendar was unavailable.
    canvas_items: [{name, course, due, overdue}], or None if Canvas was unavailable.
    """
    known = tasks is not None
    tasks = [t for t in (tasks or []) if t.get("status", "open") == "open"]
    order = {"high": 0, "normal": 1, "low": 2}
    tasks.sort(key=lambda t: (t.get("due") or "9999-99-99", order.get(t.get("priority"), 1)))
    overdue = [t for t in tasks if t.get("due") and t["due"] < today]
    due_today = [t for t in tasks if t.get("due") == today]
    later = [t for t in tasks if t not in overdue and t not in due_today]
    school = [c for c in (canvas_items or []) if c.get("overdue") or c.get("due") == today]

    spoken: list[str] = []
    if not known:
        pass
    elif not tasks:
        spoken.append("Mission log: no open missions saved.")
    else:
        parts = []
        if overdue:
            parts.append(f"{len(overdue)} overdue: " + "; ".join(_clean(t['title'], 70) for t in overdue[:SPOKEN_ITEMS]))
        if due_today:
            room = max(1, SPOKEN_ITEMS - min(len(overdue), SPOKEN_ITEMS))
            parts.append(f"{len(due_today)} due today: " + "; ".join(_clean(t['title'], 70) for t in due_today[:room]))
        if not overdue and not due_today:
            nxt = later[0]
            when = f"due {nxt['due']}" if nxt.get("due") else "no deadline"
            parts.append(f"nothing due today; next up is {_clean(nxt['title'], 70)} ({when})")
        spoken.append(f"Mission log ({len(tasks)} open): " + ". ".join(parts) + ".")
    if events:
        spoken.append("Calendar today: " + "; ".join(f"{c} {_clean(t, 60)}" for c, t in events[:4]) + ".")
    elif events is not None:
        spoken.append("Nothing on the calendar today.")
    if school:
        spoken.append("School work due: " + "; ".join(
            f"{_clean(c['name'], 60)}" + (" (overdue)" if c.get("overdue") else "") for c in school[:3]) + ".")

    if not spoken:
        return "", ""                    # nothing real to say: plain greeting
    lines = [f"MISSION LOG · {today}", ""]
    if known and not tasks:
        lines.append("No open missions. Say \"add a mission\" to start one.")
    for label, group in (("OVERDUE", overdue), ("TODAY", due_today), ("NEXT", later)):
        if group:
            lines.append(label)
            for t in group[:PANEL_ITEMS]:
                extra = " · ".join(x for x in (t.get("due") if label != "TODAY" else "",
                                               _clean(t.get("project"), 40),
                                               "high priority" if t.get("priority") == "high" else "") if x)
                lines.append(f"  #{t.get('id', '?')}  {_clean(t.get('title'))}" + (f"  ({extra})" if extra else ""))
            lines.append("")
    if events:
        lines.append("CALENDAR")
        lines.extend(f"  {c}  {_clean(t, 80)}" for c, t in events[:8])
        lines.append("")
    if school:
        lines.append("SCHOOL (CANVAS)")
        lines.extend(f"  {'Overdue' if c.get('overdue') else 'Today'}  {_clean(c.get('name'))}  · {_clean(c.get('course'), 40)}"
                     for c in school[:6])
    return " ".join(spoken), "\n".join(lines).strip()[:3600]


def gather(timeout: float = GATHER_SECONDS) -> tuple[list, list | None, list | None]:
    """(tasks, None, canvas_items) — events slot kept for compose() — each source given at most `timeout` seconds in total."""
    from plugins import daily_briefing as brief

    def tasks():
        found, notice = brief._missions()
        return None if notice else found

    def canvas():
        found, notice = brief._canvas()
        return None if notice else found

    # Daemon threads, not a pool: a source that hangs (a slow network call)
    # must never hold up quitting the app.
    results: dict = {}

    def runner(name, fn):
        try:
            results[name] = fn()
        except Exception:
            pass

    threads = [threading.Thread(target=runner, args=(n, fn), daemon=True, name=f"startup-brief-{n}")
               for n, fn in (("tasks", tasks), ("canvas", canvas))]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return results.get("tasks"), None, results.get("canvas")


def build(timeout: float = GATHER_SECONDS) -> tuple[str, str]:
    tasks, events, canvas = gather(timeout)
    return compose(tasks, events, canvas, date.today().isoformat())
