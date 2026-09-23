"""A read-only, on-demand briefing from local tasks, Mac Calendar, and Gmail.

No sign-in flow, scheduling, or email sending happens here. Calendar access may
trigger a macOS Automation permission prompt the first time it is requested.
"""
from __future__ import annotations

import platform
import subprocess
from datetime import date

from memory.config_manager import get_plugin_enabled


PLUGIN = {
    "name": "daily_briefing",
    "description": (
        "Give the user's on-demand daily/morning briefing: today's Mac Calendar "
        "events, live Canvas assignments, overdue and upcoming Mission Control "
        "tasks, and unread personal "
        "and school Gmail. Use for 'give me my daily briefing', 'what is my day "
        "looking like', 'morning update', or 'what do I have today'. Unlike "
        "mission_control's task-only briefing, this combines available sources. "
        "Read-only: never create tasks, send mail or claim to read Canvas. "
        "If the user asks for only one source, use that source's own tool."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}


_CALENDAR_SCRIPT = '''
tell application "Calendar"
    set dayStart to current date
    set time of dayStart to 0
    set dayEnd to dayStart + (1 * days)
    set foundLines to {}
    repeat with oneCalendar in calendars
        set todaysEvents to (every event of oneCalendar whose start date is greater than or equal to dayStart and start date is less than dayEnd)
        repeat with oneEvent in todaysEvents
            if allday event of oneEvent then
                set eventTime to "All day"
            else
                set eventTime to (hours of (start date of oneEvent)) as text
                set eventTime to eventTime & ":" & (minutes of (start date of oneEvent)) as text
            end if
            set end of foundLines to eventTime & tab & (summary of oneEvent)
            if (count of foundLines) is greater than or equal to 30 then exit repeat
        end repeat
        if (count of foundLines) is greater than or equal to 30 then exit repeat
    end repeat
    set AppleScript's text item delimiters to linefeed
    return foundLines as text
end tell
'''


def _clean(value, limit=100):
    """Show external content as one bounded line, without treating it as commands."""
    return " ".join(str(value or "").split())[:limit]


def _calendar():
    if platform.system() != "Darwin":
        return [], "Mac Calendar is available only on macOS."
    try:
        result = subprocess.run(
            ["osascript", "-e", _CALENDAR_SCRIPT], capture_output=True,
            text=True, timeout=12, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "Calendar could not be reached. Check that it is open and Jarvis has Automation access."
    if result.returncode:
        return [], "Calendar access is unavailable. Allow Jarvis's Python app to control Calendar in macOS settings."
    events = []
    for line in result.stdout.splitlines():
        clock, sep, title = line.partition("\t")
        if sep and title.strip():
            events.append((_clean(clock, 14), _clean(title, 100)))
    events.sort(key=lambda item: (item[0] != "All day", item[0]))
    return events[:8], None


def _missions():
    if not get_plugin_enabled("mission_control"):
        return [], "Mission Control is disabled."
    try:
        from plugins import mission_control as mission
        with mission._transaction():
            tasks = mission._load()["tasks"]
        active = sorted((t for t in tasks if t["status"] == "open"), key=mission._sort)
        return active, None
    except (OSError, ValueError, KeyError):
        return [], "Mission Control tasks could not be read; its save file was left unchanged."


def _gmail():
    if not get_plugin_enabled("gmail"):
        return [], ["Gmail is disabled."]
    try:
        from plugins import gmail
        connected = gmail._connected_accounts()
    except (ImportError, OSError):
        return [], ["Gmail is unavailable."]
    if not connected:
        return [], ["No Gmail account is connected. Ask Jarvis to connect personal or school Gmail."]
    found, notices = [], []
    for account in connected:
        try:
            service = gmail._service(account, allow_login=False)
            api = service.users().messages()
            listing = api.list(userId="me", labelIds=["INBOX"], q="is:unread", maxResults=4).execute()
            messages = listing.get("messages", []) or []
            subjects = []
            for item in messages[:3]:
                metadata = api.get(userId="me", id=item["id"], format="metadata",
                                   metadataHeaders=["From", "Subject"]).execute()
                headers = gmail._headers(metadata)
                subjects.append((_clean(headers.get("from", "Unknown sender"), 65),
                                 _clean(headers.get("subject", "No subject"), 95)))
            found.append((account, len(messages), subjects))
        except Exception:
            # Do not leak token, API response, or OAuth details into the briefing.
            notices.append(f"{account.title()} Gmail could not be read; reconnect it if needed.")
    return found, notices


def _focus_today():
    if not get_plugin_enabled("pomodoro"):
        return None
    try:
        from plugins import pomodoro
        stats = pomodoro._load_stats()
        return int(stats.get("minutes", 0))
    except (OSError, ValueError, TypeError):
        return None


def _canvas():
    if not get_plugin_enabled("canvas_school"):
        return [], "Canvas is disabled."
    try:
        from plugins.canvas_school import fetch_assignments
        return fetch_assignments()
    except Exception:
        return [], "Canvas assignments could not be read."


def run(parameters: dict, player=None, session_memory=None) -> str:
    today = date.today().isoformat()
    events, calendar_notice = _calendar()
    tasks, task_notice = _missions()
    canvas_items, canvas_notice = _canvas()
    mail, mail_notices = _gmail()
    focus = _focus_today()

    lines = [f"DAILY BRIEFING · {today}", "", "CALENDAR"]
    if calendar_notice:
        lines.append(calendar_notice)
    else:
        lines.extend(f"{clock}  {title}" for clock, title in events)
        if not events:
            lines.append("No events on today's Mac Calendar.")
        if len(events) == 8:
            lines.append("Showing the first 8 events.")

    lines += ["", "MISSIONS"]
    if task_notice:
        lines.append(task_notice)
    else:
        due = [task for task in tasks if task.get("due") and task["due"] <= today]
        upcoming = [task for task in tasks if not task.get("due") or task["due"] > today]
        selected = (due + upcoming)[:5]
        for task in selected:
            when = task.get("due") or "No deadline"
            label = ("Overdue" if task.get("due") and when < today else
                     "Today" if when == today else when)
            lines.append(f"#{task['id']}  {_clean(task['title'], 100)}  ·  {label}")
        if not selected:
            lines.append("No open missions saved.")
        elif len(tasks) > len(selected):
            lines.append(f"Showing {len(selected)} of {len(tasks)} open missions.")
        lines.append("Mission Control contains tasks you saved manually.")

    lines += ["", "CANVAS ASSIGNMENTS"]
    if canvas_notice:
        lines.append(canvas_notice)
    elif not canvas_items:
        lines.append("No incomplete or missing Canvas work was returned.")
    else:
        for item in canvas_items[:5]:
            when = "Overdue" if item["overdue"] else item["due"] or "No due date"
            lines.append(f"{when}  {_clean(item['name'], 90)}  ·  {_clean(item['course'], 40)}")
        if len(canvas_items) > 5:
            lines.append(f"Showing 5 of {len(canvas_items)} Canvas items.")

    lines += ["", "UNREAD GMAIL"]
    for account, count, subjects in mail:
        lines.append(f"{account.title()}: {'at least 4' if count == 4 else count} unread inbox emails")
        lines.extend(f"  {_clean(sender, 65)} — {_clean(subject, 95)}" for sender, subject in subjects)
    lines.extend(mail_notices)
    if focus is not None:
        lines += ["", f"FOCUS TODAY  ·  {focus} completed minutes"]

    panel = "\n".join(lines)[:3600]
    if player:
        try:
            player.show_content("DAILY BRIEFING", panel)
        except Exception:
            pass

    if task_notice:
        mission_phrase = "I couldn't read Mission Control"
    else:
        urgent = sum(bool(t.get("due")) and t["due"] <= today for t in tasks)
        mission_phrase = f"{urgent} missions due or overdue"
    calendar_phrase = (f"{len(events)} calendar events" if not calendar_notice
                       else "Calendar unavailable")
    canvas_phrase = (f"{len(canvas_items)} Canvas items" if not canvas_notice
                     else "Canvas unavailable")
    mail_phrase = ", ".join(f"{account}: {count}{'+' if count == 4 else ''} unread"
                             for account, count, _ in mail) or "no connected inbox could be read"
    return (f"Here's your briefing for {today}: {calendar_phrase}, {mission_phrase}, "
            f"{canvas_phrase}, "
            f"and {mail_phrase}. I've put the details on screen.")
