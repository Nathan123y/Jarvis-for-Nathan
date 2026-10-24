"""Read-only data for the phone texts: calendar, weather, the trading day, the morning text.

Calendar comes from Calendar's own database on this Mac, whose occurrence cache
already expands repeating events (AppleScript's Calendar search skips repeats, and
EventKit refuses a background process without ever asking). Its "Scheduled
Reminders" calendar carries Apple Reminders that have a time.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

APPLE_EPOCH = 978307200
CALENDAR_DB = Path.home() / "Library" / "Group Containers" / "group.com.apple.calendar" / "Calendar.sqlitedb"
SKIP_CALENDARS = ("holiday", "birthday", "siri suggestions", "found in natural language")


def _short(value, limit=60) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ── calendar ─────────────────────────────────────────────────────────────────
def calendar_events(start: datetime, end: datetime, db: Path = CALENDAR_DB) -> list[dict]:
    """[{"at": datetime, "title", "all_day", "calendar"}] starting in [start, end), soonest first."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT COALESCE(o.occurrence_start_date, o.occurrence_date), i.summary, i.all_day, c.title "
            "FROM OccurrenceCache o JOIN CalendarItem i ON i.ROWID = o.event_id "
            "JOIN Calendar c ON c.ROWID = o.calendar_id "
            "WHERE COALESCE(o.occurrence_start_date, o.occurrence_date) >= ? "
            "AND COALESCE(o.occurrence_start_date, o.occurrence_date) < ? ORDER BY 1",
            (start.timestamp() - APPLE_EPOCH, end.timestamp() - APPLE_EPOCH)).fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for ts, title, all_day, cal in rows:
        if not title or any(s in str(cal or "").lower() for s in SKIP_CALENDARS):
            continue
        at = datetime.fromtimestamp(ts + APPLE_EPOCH)
        key = (at, title)
        if key in seen:                       # the same event synced into two calendars
            continue
        seen.add(key)
        out.append({"at": at, "title": _short(title, 70), "all_day": bool(all_day), "calendar": cal or ""})
    return out


def clock(at: datetime) -> str:
    return at.strftime("%-I:%M %p").replace(":00 ", " ")


# ── weather (Open-Meteo: free, no key) ───────────────────────────────────────
_SKY = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers",
        95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms"}


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def weather(city: str, get=_get_json) -> Optional[str]:
    """'San Jose: 72°/54°, partly cloudy, 10% rain' for today, or None."""
    if not city:
        return None
    place = (get("https://geocoding-api.open-meteo.com/v1/search?" +
                 urlencode({"name": city.split(",")[0].strip(), "count": 1})).get("results") or [None])[0]
    if not place:
        return None
    d = get("https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 1,
        "temperature_unit": "fahrenheit",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}))["daily"]
    hi, lo = round(d["temperature_2m_max"][0]), round(d["temperature_2m_min"][0])
    sky = _SKY.get(int(d["weather_code"][0]), "mixed")
    rain = d.get("precipitation_probability_max", [None])[0]
    return f"{place['name']}: {hi}°/{lo}°, {sky}" + (f", {rain}% rain" if rain else "")


# ── trading ──────────────────────────────────────────────────────────────────
BOTS = Path.home() / "optionsbots" / "scripts" / "bots.py"


def trading_summary(run=subprocess.run) -> Optional[str]:
    """The day's paper results, options and memecoins, in the bots' own words, plus the best and
    worst options bot today. None when the bots aren't installed."""
    if not BOTS.exists():
        return None
    out = run([sys.executable, str(BOTS), "portfolio", "--jarvis"], capture_output=True, text=True, timeout=150)
    text = " ".join((out.stdout or "").split())
    from core.away import optionsbots_brief
    try:
        trades = (optionsbots_brief().get("today") or {}).get("closed_trades") or []
    except Exception:
        trades = []
    by_bot: dict = {}
    for t in trades:
        try:
            by_bot[t.get("bot_id")] = by_bot.get(t.get("bot_id"), 0.0) + float(t["pnl"])
        except (KeyError, TypeError, ValueError):
            pass
    if len(by_bot) >= 2:
        best = max(by_bot, key=by_bot.get)
        worst = min(by_bot, key=by_bot.get)
        text += f" Best bot: {best} ({by_bot[best]:+,.0f}). Worst: {worst} ({by_bot[worst]:+,.0f})."
    return text or None


# ── fantasy football ─────────────────────────────────────────────────────────
def fantasy_summary() -> Optional[str]:
    """This week's score and projection in each league, then players to swap out or watch."""
    from plugins import espn_fantasy as espn
    lines = []
    for lg in espn._leagues():
        try:
            me, them = espn._week_sides(lg)
        except ValueError:
            continue
        lead = me[2] - them[2]
        call = f"projected to win by {lead:.1f}" if lead > 0 else \
            f"projected to lose by {-lead:.1f}" if lead < 0 else "projected even"
        lines.append(f"{_short(espn._name(lg), 25)}: you {me[1]:.1f} vs {_short(them[0].team_name, 25)} "
                     f"{them[1]:.1f}, {call}.")
    if not lines:
        return None
    found = espn.roster_alerts()
    if found.get("swap"):
        lines.append("Swap out: " + "; ".join(found["swap"][:4]))
    if found.get("watch"):
        lines.append("Questionable: " + "; ".join(found["watch"][:4]))
    if not found.get("swap") and not found.get("watch"):
        lines.append("No injured or bye-week starters.")
    return "\n".join(lines)


# ── the morning text ─────────────────────────────────────────────────────────
def morning_text(*, today: date, weather_line: Optional[str], events: Optional[list[dict]], canvas: list[dict],
                 fantasy_swaps: list[str], packages: list[str], overnight: list[str]) -> str:
    lines = [f"Good morning! {today.strftime('%a %b %-d')}"]
    if weather_line:
        lines.append(f"☀️ {weather_line}")
    if events is None:
        lines.append("📅 Calendar unavailable (Jarvis's worker needs Full Disk Access).")
    elif events:
        lines.append("📅 " + "; ".join(("All day " if e["all_day"] else clock(e["at"]) + " ") + e["title"]
                                       for e in events[:6]))
    else:
        lines.append("📅 Nothing on your calendar.")
    tomorrow = (today + timedelta(days=1)).isoformat()
    due_today = [c for c in canvas if c.get("due") == today.isoformat()]
    due_tomorrow = [c for c in canvas if c.get("due") == tomorrow]
    overdue = [c for c in canvas if c.get("overdue")]
    week = (today + timedelta(days=5)).isoformat()
    soon = [c for c in canvas if not c.get("overdue") and tomorrow < (c.get("due") or "") <= week]
    for label, items in (("Overdue", overdue), ("Due today", due_today), ("Due tomorrow", due_tomorrow)):
        if items:
            lines.append(f"📚 {label}: " + "; ".join(f"{_short(c['name'], 45)} ({_short(c['course'], 20)})"
                                                    for c in items[:4]))
    if soon:
        lines.append("📚 Coming up: " + "; ".join(
            f"{_short(c['name'], 40)} ({date.fromisoformat(c['due']).strftime('%a')})" for c in soon[:3]))
    if fantasy_swaps:
        lines.append("🏈 Swap out: " + "; ".join(fantasy_swaps[:3]))
    if packages:
        lines.append("📦 " + "; ".join(packages[:3]))
    if overnight:
        lines.append("🌙 Overnight: " + " | ".join(overnight[:5]))
    return "\n".join(lines)
