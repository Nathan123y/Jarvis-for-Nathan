"""Personal, read-only Canvas LMS assignment connection for Jarvis.

The user enters their own Canvas domain and personal access token in Plugin
Settings on their Mac. Both are kept in Jarvis's ignored local config file.
Canvas's planner (incomplete work) and missing-submissions endpoints supply
upcoming and overdue work without changing anything in Canvas.
"""
from __future__ import annotations

import ipaddress
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from memory.config_manager import get_plugin_config


PLUGIN = {
    "name": "canvas_school",
    "description": (
        "Read the user's Canvas LMS school assignments. Use for 'what is due on "
        "Canvas', 'check my Canvas assignments', 'show overdue Canvas work', "
        "'what is due on Canvas next month', "
        "'did I submit the lab', 'what haven't I handed in', "
        "or 'connect Canvas'. It shows whether each assignment is submitted, graded, "
        "late or missing. For a combined calendar, Gmail and school update, "
        "use daily_briefing instead. Canvas access is read-only. Never ask the "
        "user to say their token or calendar feed link aloud or paste it in chat. "
        "Connection details go into Jarvis Plugin Settings on their Mac."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "assignments (default: what's due and what's submitted) or connect (opens a Chrome window to log into Canvas once)"},
            "timeframe": {"type": "STRING", "description": "upcoming (default, next 14 days), this_month, or next_month. For 'next month' choose next_month."},
        },
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


# ── logged-in browser (no API token needed) ─────────────────────────────────
# Many schools, SJSU included, don't let students make API tokens, and the
# calendar feed has no submissions in it. So Jarvis keeps its own Chrome profile
# logged into Canvas (like the TikTok one) and reads the same JSON the Canvas
# web app reads, with that session. Read-only: only GET requests are made.

from pathlib import Path as _Path

_BASE = _Path(__file__).resolve().parent.parent
PROFILE = _BASE / "memory" / "canvas_profile"
_MARKER = PROFILE / ".jarvis_connected"
_COOKIES = PROFILE / ".jarvis_cookies.json"     # session cookies Chrome would drop on close
_CACHE = _BASE / "memory" / "canvas_cache.json"
_LOCK = _BASE / "memory" / "canvas_profile.lock"
CACHE_SECONDS = 600


class NotLoggedIn(RuntimeError):
    pass


def _browser_base(value):
    """The Canvas site from what the user typed, even with a path on the end
    (e.g. https://sjsu.instructure.com/courses)."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("Enter your school's Canvas website in Jarvis Plugin Settings.")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    return _base_url(f"{parts.scheme}://{parts.hostname or ''}")


def browser_connected() -> bool:
    import sys
    if "unittest" in (sys.argv[0] if sys.argv else "") or "pytest" in sys.modules:
        return False        # a test must never open the real logged-in browser
    return _MARKER.exists()


def _in_thread(fn):
    """Run Playwright's sync API on a thread of its own: it refuses to start on a
    thread that already runs an asyncio loop (the voice session's)."""
    import threading
    box = {}

    def go():
        try:
            box["ok"] = fn()
        except BaseException as exc:      # handed back to the caller below
            box["err"] = exc
    t = threading.Thread(target=go, name="canvas-browser", daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def _context(p, visible):
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=not visible,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"], viewport={"width": 1200, "height": 850})


def _save_cookies(ctx) -> None:
    """Keep the session cookies: Canvas and the school login use cookies that
    end with the browser, so without this every read would be logged out."""
    import json
    import os
    try:
        tmp = _COOKIES.with_suffix(".tmp")
        tmp.write_text(json.dumps(ctx.cookies()), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_COOKIES)
    except Exception as exc:
        print(f"[Canvas] could not keep the login cookies: {exc!r}")


def _restore_cookies(ctx) -> None:
    import json
    try:
        saved = json.loads(_COOKIES.read_text(encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    keep = [c for c in saved if isinstance(c, dict) and (c.get("expires", -1) in (-1, None) or c["expires"] > now)]
    for c in keep:
        if c.get("expires") is None:
            c["expires"] = -1
    try:
        ctx.add_cookies(keep)
    except Exception as exc:
        print(f"[Canvas] could not restore the login cookies: {exc!r}")


def _json(response):
    """Canvas prefixes session-authenticated JSON with 'while(1);'."""
    text = response.text()
    if text.startswith("while(1);"):
        text = text[len("while(1);"):]
    import json
    return json.loads(text)


def _api_ok(ctx, base) -> bool:
    try:
        r = ctx.request.get(base + "/api/v1/users/self", headers={"Accept": "application/json"},
                            max_redirects=0, timeout=15000)
        return r.status == 200 and isinstance(_json(r), dict)
    except Exception:
        return False


def _browser_pages(ctx, base, path, params=None):
    collected, url = [], base + path
    for _ in range(4):
        r = ctx.request.get(url, params=params, headers={"Accept": "application/json"},
                            max_redirects=0, timeout=20000)
        if r.status in (401, 403):
            raise NotLoggedIn("Canvas session ended")
        if r.status != 200:
            raise ValueError("Canvas returned an error. Try again in a minute.")
        payload = _json(r)
        if not isinstance(payload, list):
            raise ValueError("Canvas returned an unexpected response.")
        collected.extend(i for i in payload if isinstance(i, dict))
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
        if not nxt:
            break
        parts = urlsplit(nxt.group(1))
        if (parts.scheme != "https" or parts.netloc.lower() != urlsplit(base).netloc.lower()
                or not parts.path.startswith("/api/v1/")):
            raise ValueError("Canvas returned an unsafe pagination link; stopped reading.")
        url, params = nxt.group(1), None
    return collected[:200]


def canvas_login(domain, wait_s: int = 900) -> bool:
    """Open Canvas in a Chrome window and wait for the user to log in (SSO, Duo).
    True once Canvas answers its API with that session."""
    base = _browser_base(domain)

    def go():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _context(p, visible=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(base + "/login", wait_until="domcontentloaded", timeout=60000)
                end = time.time() + wait_s
                while time.time() < end:
                    if _api_ok(ctx, base):
                        _save_cookies(ctx)
                        _MARKER.write_text(base, encoding="utf-8")
                        try:
                            _CACHE.unlink()
                        except FileNotFoundError:
                            pass
                        page.wait_for_timeout(1500)
                        return True
                    page.wait_for_timeout(2500)
                return False
            finally:
                ctx.close()
    with _profile_lock():
        return bool(_in_thread(go))


def _profile_lock():
    """One process at a time in the Chrome profile (the app and the worker both read Canvas)."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        _LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    return held()


def _read_cache(key):
    import json
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("key") == key and time.time() - float(data.get("at") or 0) < CACHE_SECONDS:
        return data.get("items")
    return None


def _write_cache(key, items):
    import json
    try:
        tmp = _CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "at": time.time(), "items": items}), encoding="utf-8")
        tmp.replace(_CACHE)
    except Exception:
        pass


def last_known_status():
    """What Canvas said at the last read (no network): (items, unix time) or ([], 0)."""
    import json
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        return list(data.get("items") or []), float(data.get("at") or 0)
    except Exception:
        return [], 0.0


def browser_status(domain, start, end):
    """Assignment statuses read through the logged-in Chrome profile."""
    base = _browser_base(domain)
    key = f"{base}|{start}|{end}"
    cached = _read_cache(key)
    if cached is not None:
        return cached

    def go():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _context(p, visible=False)
            try:
                _restore_cookies(ctx)
                if not _api_ok(ctx, base):
                    # The Canvas session may have lapsed while the school login is
                    # still remembered: visiting Canvas signs straight back in.
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    try:
                        page.goto(base + "/", wait_until="networkidle", timeout=45000)
                    except Exception:
                        pass
                    if not _api_ok(ctx, base):
                        raise NotLoggedIn("Canvas needs a fresh login")
                items = _status_from(lambda path, params: _browser_pages(ctx, base, path, params), start, end)
                _save_cookies(ctx)          # Canvas refreshes its session as it is used
                return items
            finally:
                ctx.close()
    with _profile_lock():
        cached = _read_cache(key)          # another process may have just read it
        if cached is not None:
            return cached
        items = _in_thread(go)
    _write_cache(key, items)
    return items


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


def _feed_time(value):
    """Local ISO date-time for a timed DTSTART, "" for an all-day one."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
        if re.fullmatch(r"\d{8}T\d{6}", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%S").isoformat(timespec="minutes")
    except ValueError:
        pass
    return ""


def _local_time(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.isoformat(timespec="minutes")


def _date_range(timeframe):
    today = date.today()
    if timeframe == "upcoming":
        return today, today + timedelta(days=14)
    first = today.replace(day=1)
    next_first = (first.replace(year=first.year + 1, month=1)
                  if first.month == 12 else first.replace(month=first.month + 1))
    if timeframe == "this_month":
        return today, next_first - timedelta(days=1)
    if timeframe == "next_month":
        after_next = (next_first.replace(year=next_first.year + 1, month=1)
                      if next_first.month == 12 else next_first.replace(month=next_first.month + 1))
        return next_first, after_next - timedelta(days=1)
    raise ValueError("Choose upcoming, this_month, or next_month for Canvas timeframe.")


def _feed_assignments(url, start=None, end=None):
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
    start = start or date.today()
    end = end or start + timedelta(days=14)
    items, event = [], None
    for line in unfolded.splitlines():
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT" and event is not None:
            due = _feed_day(event.get("DTSTART", ""))
            if due and start <= date.fromisoformat(due) <= end and event.get("SUMMARY"):
                items.append({"name": _clean(_ical_unescape(event["SUMMARY"])),
                              "course": _clean(_ical_unescape(event.get("LOCATION", "School")), 45),
                              "due": due, "due_at": _feed_time(event.get("DTSTART", "")),
                              "overdue": False})
            event = None
        elif event is not None and ":" in line:
            key, value = line.split(":", 1)
            event.setdefault(key.split(";", 1)[0].upper(), value)
    items.sort(key=lambda item: (item["due"], item["name"]))
    return items[:20]


def _work_status(raw, today):
    """Plain status of one planner item from Canvas's own submission flags."""
    override = raw.get("planner_override") or {}
    subs = raw.get("submissions")
    subs = subs if isinstance(subs, dict) else {}
    work = raw.get("plannable") if isinstance(raw.get("plannable"), dict) else {}
    due = _local_day(work.get("due_at") or raw.get("plannable_date") or work.get("todo_date"))
    if subs.get("excused"):
        return "excused"
    if subs.get("graded") and subs.get("submitted", True):
        return "submitted, graded"
    if subs.get("submitted"):
        return "submitted late" if subs.get("late") else "submitted"
    if subs.get("graded"):
        return "graded"
    if subs.get("missing"):
        return "missing"
    if isinstance(override, dict) and override.get("marked_complete"):
        return "marked done"
    if due and due < today:
        return "not submitted, past due"
    return "not submitted yet"


DONE = ("submitted", "submitted late", "submitted, graded", "graded", "excused", "marked done")


def _status_from(get, start, end):
    """Every assignment, quiz and discussion in the window, with its status, plus
    anything older Canvas lists as missing. get(path, params) -> list of dicts."""
    today = date.today().isoformat()
    upcoming = get("/api/v1/planner/items", {
        "start_date": start.isoformat(), "end_date": end.isoformat(), "per_page": 100})
    missing = get("/api/v1/users/self/missing_submissions", {"per_page": 50, "include[]": "course"})
    results, seen = [], {}
    for raw in upcoming:
        kind = str(raw.get("plannable_type") or "").lower()
        if kind not in ("assignment", "quiz", "discussion_topic"):
            continue
        work = raw.get("plannable")
        if not isinstance(work, dict):
            continue
        key = (str(raw.get("course_id")), str(raw.get("plannable_id") or work.get("id")))
        if key in seen:
            continue
        status = _work_status(raw, today)
        due = _local_day(work.get("due_at") or raw.get("plannable_date") or work.get("todo_date"))
        results.append({"name": _clean(work.get("name") or work.get("title") or "Course work"),
                        "course": _clean(raw.get("context_name") or raw.get("context_code")
                                         or "School", 45),
                        "due": due, "due_at": _local_time(work.get("due_at")),
                        "status": status,
                        "overdue": status in ("missing", "not submitted, past due")})
        seen[key] = results[-1]
    for raw in missing:
        assignment_id = raw.get("id")
        if assignment_id is None:
            continue
        key = (str(raw.get("course_id")), str(assignment_id))
        if key in seen:
            if seen[key]["status"] not in DONE:      # Canvas itself says it's missing
                seen[key].update(status="missing", overdue=True)
            continue
        results.append({"name": _clean(raw.get("name") or "Assignment"),
                        "course": _clean((raw.get("course") or {}).get("name")
                                         if isinstance(raw.get("course"), dict) else "School", 45),
                        "due": _local_day(raw.get("due_at")), "due_at": _local_time(raw.get("due_at")),
                        "status": "missing", "overdue": True})
        seen[key] = results[-1]
    results.sort(key=lambda item: (not item["overdue"], item["due"] or "9999-99-99"))
    return results


def _status_range(timeframe):
    """The window for a status check: the requested range, plus the last two weeks
    when it starts today, so "did I hand in last week's lab" is answerable."""
    start, end = _date_range(timeframe)
    if timeframe in ("upcoming", "this_month"):
        start = start - timedelta(days=14)
    return start, end


def fetch_status(timeframe="upcoming"):
    """(items with a "status", notice, mode). mode: "token", "browser" or "feed".
    Feed items have status "unknown": the calendar feed carries no submissions."""
    try:
        start, end = _status_range(timeframe)
    except ValueError as exc:
        return [], str(exc), ""
    config = get_plugin_config("canvas")
    token = str(config.get("token") or "").strip()
    if token:
        try:
            base = _base_url(config.get("domain"))
            return _status_from(lambda path, params: _request_pages(base, token, path, params),
                                start, end)[:40], None, "token"
        except ValueError as exc:
            return [], str(exc), "token"
        except Exception:
            return [], "Canvas couldn't be reached right now. Check your connection and try again.", "token"
    if browser_connected():
        try:
            return browser_status(config.get("domain"), start, end)[:40], None, "browser"
        except NotLoggedIn:
            return [], ("Canvas logged Jarvis out. Say 'connect Canvas' and log in once in the "
                        "window that opens."), "browser"
        except ValueError as exc:
            return [], str(exc), "browser"
        except Exception as exc:
            print(f"[Canvas] browser read failed: {type(exc).__name__}: {str(exc)[:120]}")
            return [], "Canvas couldn't be reached right now. Check your connection and try again.", "browser"
    feed = str(config.get("calendar_feed") or "").strip()
    if not feed:
        return [], "Canvas isn't connected. Say 'connect Canvas' and log in once in the window that opens.", ""
    try:
        f_start, f_end = _date_range(timeframe)
        items = _feed_assignments(_calendar_url(feed), f_start, f_end)
    except ValueError as exc:
        return [], str(exc), "feed"
    except Exception:
        return [], "Canvas Calendar Feed couldn't be reached. Check your connection and try again.", "feed"
    for item in items:
        item["status"] = "unknown"
    return items, None, "feed"


def fetch_assignments(timeframe="upcoming"):
    """Work still to do: (items, notice). Submitted, graded, excused and
    marked-done work is left out. Never returns tokens or raw HTTP errors."""
    items, notice, mode = fetch_status(timeframe)
    if notice:
        return [], notice
    if mode == "feed":
        return items, None
    try:
        first, _ = _date_range(timeframe)
    except ValueError:
        first = date.today()
    todo = [i for i in items if i["status"] not in DONE
            and (i["overdue"] or not i["due"] or i["due"] >= first.isoformat())]
    return todo[:20], None


def fetch_grades(since):
    """Submissions graded after `since` (an aware datetime), as (items, notice). Needs the API token;
    the calendar feed has no grades."""
    config = get_plugin_config("canvas")
    token = str(config.get("token") or "").strip()
    if not token:
        return [], "Canvas grades need a personal access token in Plugin Settings; the calendar feed has no grades."
    try:
        base = _base_url(config.get("domain"))
        courses = _request_pages(base, token, "/api/v1/courses",
                                 {"enrollment_state": "active", "per_page": 50})
        results = []
        for course in courses[:15]:
            if course.get("id") is None:
                continue
            for sub in _request_pages(base, token, f"/api/v1/courses/{int(course['id'])}/students/submissions", {
                    "student_ids[]": "self", "graded_since": since.isoformat(),
                    "include[]": "assignment", "per_page": 50}):
                if sub.get("excused") or sub.get("workflow_state") != "graded" or sub.get("id") is None:
                    continue
                work = sub.get("assignment") if isinstance(sub.get("assignment"), dict) else {}
                results.append({"id": str(sub["id"]), "course": _clean(course.get("name") or "School", 45),
                                "name": _clean(work.get("name") or "Assignment"),
                                "score": sub.get("score"), "possible": work.get("points_possible"),
                                "grade": _clean(sub.get("grade"), 20), "graded_at": _local_time(sub.get("graded_at"))})
    except ValueError as exc:
        return [], str(exc)
    except Exception:
        return [], "Canvas couldn't be reached right now."
    return results, None


def _start_login(domain) -> str:
    import threading
    try:
        _browser_base(domain)
    except ValueError as exc:
        return str(exc)

    def go():
        try:
            ok = canvas_login(domain)
            print("[Canvas] logged in" if ok else "[Canvas] login window timed out")
        except Exception as exc:
            print(f"[Canvas] login failed: {type(exc).__name__}: {str(exc)[:120]}")
    threading.Thread(target=go, name="canvas-login", daemon=True).start()
    return ("A Chrome window is opening on Canvas. The user logs in there once with their school "
            "account and Duo; Jarvis stays logged in after that and can see what is submitted. "
            "Tell them that in a sentence.")


def _when(item):
    return item["due"] or "no due date"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "assignments").lower().strip()
    timeframe = str((parameters or {}).get("timeframe") or "upcoming").lower().strip()
    config = get_plugin_config("canvas")
    if action == "connect":
        if str(config.get("token") or "").strip():
            return "Canvas is already connected with an access token, which shows submissions."
        return _start_login(config.get("domain") or "")
    if action not in ("assignments", "check", "status", "submitted"):
        return "Canvas actions: assignments, check, or connect."
    items, notice, mode = fetch_status(timeframe)
    if notice:
        return notice
    if not items:
        return f"Canvas is connected. Nothing on Canvas for {timeframe.replace('_', ' ')}."
    if mode == "feed":
        lines = [f"CANVAS CALENDAR · {timeframe.replace('_', ' ')} (submission status unknown)"]
        lines += [f"{_when(i)} · {i['name']} · {i['course']}" for i in items[:12]]
        if player:
            try:
                player.show_content("CANVAS SCHOOL", "\n".join(lines)[:3500])
            except Exception:
                pass
        return ("Canvas calendar only, so submission status is unknown. To see what is submitted, "
                "the user can say 'connect Canvas' and log in once.\n" + "\n".join(lines[1:]))
    todo = [i for i in items if i["status"] not in DONE]
    done = [i for i in items if i["status"] in DONE]
    lines = [f"CANVAS · {timeframe.replace('_', ' ')} · {len(todo)} to do, {len(done)} handed in"]
    lines += [f"{i['status'].upper()} · {_when(i)} · {i['name']} · {i['course']}" for i in todo[:14]]
    lines += [f"{i['status']} · {_when(i)} · {i['name']} · {i['course']}" for i in done[:14]]
    if player:
        try:
            player.show_content("CANVAS SCHOOL", "\n".join(lines)[:3500])
        except Exception:
            pass
    return ("Canvas, read just now. Each line is status · due date · assignment · course. Answer "
            "what was asked from this; the list is on screen.\n" + "\n".join(lines))
