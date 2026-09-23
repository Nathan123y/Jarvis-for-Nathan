"""Personal, read-only Canvas LMS assignment connection for Jarvis.

The user enters their own Canvas domain and personal access token in Plugin
Settings on their Mac. Both are kept in Jarvis's ignored local config file.
Canvas's planner (incomplete work) and missing-submissions endpoints supply
upcoming and overdue work without changing anything in Canvas.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from memory.config_manager import get_plugin_config


PLUGIN = {
    "name": "canvas_school",
    "description": (
        "Read the user's Canvas LMS school assignments. Use for 'what is due on "
        "Canvas', 'check my Canvas assignments', 'show overdue Canvas work', "
        "or 'connect Canvas'. For a combined calendar, Gmail and school update, "
        "use daily_briefing instead. Canvas access is read-only. Never ask the "
        "user to say their token or calendar feed link aloud or paste it in chat. "
        "Connection details go into Jarvis Plugin Settings on their Mac."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING",
                                 "description": "assignments (default), check, or connect (setup instructions)"}},
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "canvas",
    "title": "Canvas school assignments (read-only)",
    "fields": [
        {"key": "domain", "type": "text", "label": "Canvas website",
         "placeholder": "https://your-school.instructure.com"},
        {"key": "token", "type": "password", "label": "Your personal Canvas access token",
         "placeholder": "Paste the token here on your Mac; never say it aloud"},
        {"key": "calendar_feed", "type": "password", "label": "Canvas Calendar Feed link (no API token needed)",
         "placeholder": "Paste your Calendar Feed link here; keep it private"},
    ],
}


def _base_url(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("Enter your school's Canvas website in Jarvis Plugin Settings.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or hostname == "localhost" or "." not in hostname):
        raise ValueError("Enter only your school's HTTPS Canvas website, with no path or port.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a school Canvas hostname, not an IP address.")
    return "https://" + hostname.lower()


def _request_pages(base, token, path, params=None):
    """At most three pages, without forwarding a token to a redirected host."""
    import requests
    url = base + path
    collected = []
    for _ in range(3):
        response = requests.get(url, headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/json"},
                                params=params, timeout=(4, 8), allow_redirects=False)
        if response.status_code in (401, 403):
            raise ValueError("Canvas denied access. Check your token or ask your school whether API access is allowed.")
        if response.status_code != 200:
            raise ValueError("Canvas returned an error. Check the school website and try again.")
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Canvas returned an unexpected response.")
        collected.extend(item for item in payload if isinstance(item, dict))
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            break
        parts = urlsplit(next_url)
        if (parts.scheme != "https" or parts.netloc.lower() != urlsplit(base).netloc.lower()
                or not parts.path.startswith("/api/v1/")):
            raise ValueError("Canvas returned an unsafe pagination link; stopped reading.")
        url, params = next_url, None
    return collected[:150]


def _local_day(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().date().isoformat() if parsed.tzinfo else parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _clean(value, limit=110):
    return " ".join(str(value or "").split())[:limit]


def _calendar_url(value):
    """Only fetch the HTTPS Canvas calendar feed, never arbitrary private URLs."""
    value = str(value or "").strip()
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not host or parts.username or parts.password
            or parts.port or parts.fragment or not host.endswith(".instructure.com")
            or not re.fullmatch(r"/feeds/calendars/[^/]+\.ics", parts.path)
            or len(value) > 2048):
        raise ValueError("Paste the HTTPS Canvas Calendar Feed link from Canvas Calendar into Plugin Settings.")
    return value


def _ical_unescape(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value)


def _feed_day(value):
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc).astimezone().date().isoformat()
        if re.fullmatch(r"\d{8}T\d{6}", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%S").date().isoformat()
    except ValueError:
        pass
    return ""


def _feed_assignments(url):
    import requests
    response = requests.get(url, timeout=(4, 8), allow_redirects=False,
                            headers={"Accept": "text/calendar"}, stream=True)
    if response.status_code != 200:
        response.close()
        raise ValueError("Canvas Calendar Feed could not be read. Check the link in Plugin Settings.")
    try:
        payload = bytearray()
        for chunk in response.iter_content(16384):
            payload.extend(chunk)
            if len(payload) > 2_000_000:
                raise ValueError("Canvas Calendar Feed is too large to read.")
    finally:
        response.close()
    raw = payload.decode("utf-8-sig", errors="replace")
    if "BEGIN:VCALENDAR" not in raw:
        raise ValueError("Canvas Calendar Feed returned an unexpected format.")
    # RFC 5545: a line beginning with whitespace continues the previous line.
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    today = date.today()
    deadline = today + timedelta(days=14)
    items, event = [], None
    for line in unfolded.splitlines():
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT" and event is not None:
            due = _feed_day(event.get("DTSTART", ""))
            if due and today <= date.fromisoformat(due) <= deadline and event.get("SUMMARY"):
                items.append({"name": _clean(_ical_unescape(event["SUMMARY"])),
                              "course": _clean(_ical_unescape(event.get("LOCATION", "School")), 45),
                              "due": due, "overdue": False})
            event = None
        elif event is not None and ":" in line:
            key, value = line.split(":", 1)
            event.setdefault(key.split(";", 1)[0].upper(), value)
    items.sort(key=lambda item: (item["due"], item["name"]))
    return items[:20]


def fetch_assignments():
    """Return (items, notice); never return access tokens or raw HTTP errors."""
    config = get_plugin_config("canvas")
    token = str(config.get("token") or "").strip()
    feed = str(config.get("calendar_feed") or "").strip()
    if not token:
        if not feed:
            return [], "Canvas isn't connected. Add your Canvas Calendar Feed link in Jarvis Plugin Settings."
        try:
            return _feed_assignments(_calendar_url(feed)), None
        except ValueError as exc:
            return [], str(exc)
        except Exception:
            return [], "Canvas Calendar Feed couldn't be reached. Check your connection and try again."
    try:
        base = _base_url(config.get("domain"))
        today = date.today()
        upcoming = _request_pages(base, token, "/api/v1/planner/items", {
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=14)).isoformat(),
            "filter": "incomplete_items", "per_page": 50,
        })
        missing = _request_pages(base, token, "/api/v1/users/self/missing_submissions",
                                 {"per_page": 50})
    except ValueError as exc:
        return [], str(exc)
    except Exception:
        return [], "Canvas couldn't be reached right now. Check your connection and try again."

    results, seen = [], set()
    for raw in missing:
        assignment_id = raw.get("id")
        if assignment_id is None:
            continue
        key = (str(raw.get("course_id")), str(assignment_id))
        seen.add(key)
        results.append({"name": _clean(raw.get("name") or "Assignment"),
                        "course": _clean((raw.get("course") or {}).get("name")
                                         if isinstance(raw.get("course"), dict) else "School", 45),
                        "due": _local_day(raw.get("due_at")), "overdue": True})

    for raw in upcoming:
        kind = str(raw.get("plannable_type") or "").lower()
        if kind not in ("assignment", "quiz", "discussion_topic"):
            continue
        work = raw.get("plannable")
        if not isinstance(work, dict):
            continue
        override = raw.get("planner_override") or {}
        if isinstance(override, dict) and override.get("marked_complete"):
            continue
        key = (str(raw.get("course_id")), str(raw.get("plannable_id") or work.get("id")))
        if key in seen:
            continue
        seen.add(key)
        results.append({"name": _clean(work.get("name") or work.get("title") or "Course work"),
                        "course": _clean(raw.get("context_name") or raw.get("context_code")
                                         or "School", 45),
                        "due": _local_day(work.get("due_at") or raw.get("plannable_date")
                                          or work.get("todo_date")), "overdue": False})
    results.sort(key=lambda item: (not item["overdue"], item["due"] or "9999-99-99"))
    return results[:20], None


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "assignments").lower().strip()
    if action == "connect":
        return ("In Canvas, open Calendar and click Calendar Feed. Copy the link. In Jarvis, "
                "open Settings > Plugin Settings > Canvas, paste it into Canvas Calendar Feed "
                "and press Save. Keep the link private. This shows upcoming calendar dates, "
                "but cannot tell whether you submitted an assignment. If you also have an "
                "approved API token, the existing token connection takes priority.")
    if action not in ("assignments", "check"):
        return "Canvas actions: assignments, check, or connect."
    items, notice = fetch_assignments()
    if notice:
        return notice
    if not items:
        return "Canvas is connected. No upcoming Canvas items were returned."
    feed_mode = not str(get_plugin_config("canvas").get("token") or "").strip()
    lines = ["CANVAS CALENDAR (upcoming dates; submission status unknown)" if feed_mode
             else "CANVAS ASSIGNMENTS (read-only)"]
    for item in items[:10]:
        when = "Overdue" if item["overdue"] else (item["due"] or "No due date")
        lines.append(f"{when} · {item['name']} · {item['course']}")
    if len(items) > 10:
        lines.append(f"Showing 10 of {len(items)} fetched items.")
    if player:
        try:
            player.show_content("CANVAS SCHOOL", "\n".join(lines)[:3500])
        except Exception:
            pass
    return (f"Canvas has {len(items)} {'upcoming calendar items; submission status is unknown' if feed_mode else 'incomplete or missing items'} in this view. "
            f"Next: {items[0]['name']}, "
            f"{'overdue' if items[0]['overdue'] else 'due ' + (items[0]['due'] or 'date not specified')}. "
            "I've put the list on screen.")
