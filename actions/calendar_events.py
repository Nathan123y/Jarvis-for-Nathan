"""Read-only Mac Calendar events for a selected local day."""
from __future__ import annotations

import platform
import subprocess
from datetime import date


_EVENTS_SCRIPT = '''
on run argv
    set dayOffset to (item 1 of argv) as integer
    tell application "Calendar"
        set dayStart to current date
        set time of dayStart to 0
        set dayStart to dayStart + (dayOffset * days)
        set dayEnd to dayStart + (1 * days)
        set foundLines to {}
        repeat with oneCalendar in calendars
            set selectedEvents to (every event of oneCalendar whose start date is greater than or equal to dayStart and start date is less than dayEnd)
            repeat with oneEvent in selectedEvents
                if allday event of oneEvent then
                    set eventTime to "All day"
                else
                    set eventStart to start date of oneEvent
                    set eventTime to (hours of eventStart) as text
                    set eventTime to eventTime & ":" & (minutes of eventStart) as text
                end if
                set end of foundLines to eventTime & tab & (summary of oneEvent)
                if (count of foundLines) is greater than or equal to 30 then exit repeat
            end repeat
            if (count of foundLines) is greater than or equal to 30 then exit repeat
        end repeat
        set AppleScript's text item delimiters to linefeed
        return foundLines as text
    end tell
end run
'''


def _day_offset(value: str) -> tuple[int, date]:
    today = date.today()
    text = value.strip().lower()
    if text in ("", "today"):
        return 0, today
    if text == "tomorrow":
        return 1, date.fromordinal(today.toordinal() + 1)
    selected = date.fromisoformat(text)
    offset = (selected - today).days
    if abs(offset) > 366:
        raise ValueError("Choose a date within one year of today.")
    return offset, selected


def calendar_events(parameters: dict | None = None) -> str:
    if platform.system() != "Darwin":
        return "Mac Calendar is available only on macOS."
    requested = str((parameters or {}).get("day") or "today")
    try:
        offset, selected = _day_offset(requested)
    except ValueError:
        return "Choose today, tomorrow, or a date such as 2026-10-01."
    try:
        result = subprocess.run(
            ["osascript", "-", str(offset)], input=_EVENTS_SCRIPT,
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Mac Calendar did not respond. Try again in a moment."
    if result.returncode:
        detail = (result.stderr or "").lower()
        if "-1743" in detail or "not authorized to send apple events" in detail:
            return ("macOS denied Calendar Automation. Check System Settings → Privacy "
                    "& Security → Automation for Jarvis or Python, then reopen Jarvis.")
        return "Mac Calendar could not be read. Open Calendar and try again."

    events = []
    for line in result.stdout.splitlines():
        clock, sep, title = line.partition("\t")
        if not sep or not title.strip():
            continue
        clock = " ".join(clock.split())[:20]
        title = " ".join(title.split())[:120]
        try:
            hour, minute = map(int, clock.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                continue
            label, sort_key = f"{hour:02d}:{minute:02d}", (1, hour, minute)
        except (ValueError, TypeError):
            if clock != "All day":
                continue
            label, sort_key = "All day", (0, 0, 0)
        events.append((sort_key, label, title))
    if not events:
        return f"No events on your Mac Calendar for {selected.isoformat()}."
    events.sort(key=lambda event: event[0])
    lines = [f"Mac Calendar for {selected.isoformat()}:"]
    lines.extend(f"{label} — {title}" for _, label, title in events[:10])
    if len(events) > 10:
        lines.append(f"Showing 10 of {len(events)} events.")
    return "\n".join(lines)


TOOL = {
    "name": "calendar_events",
    "description": (
        "Read the user's Mac Calendar appointments and events for today, tomorrow, "
        "or one specified date. Use for 'what's on my Calendar tomorrow?', "
        "'do I have an appointment today?', and similar Calendar questions. "
        "Does not create or change events. Use daily_briefing only for a combined briefing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "day": {"type": "STRING", "description": "today, tomorrow, or YYYY-MM-DD in the user's local time."},
        },
    },
    "handler": calendar_events,
}
