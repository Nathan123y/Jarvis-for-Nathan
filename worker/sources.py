"""Read-only data for the phone texts: calendar, weather, the trading day, the bots' nightly review,
the memecoin bot, Gumroad sales, spending, workouts, the morning text and the Sunday recap.

Calendar comes from Calendar's own database on this Mac, whose occurrence cache
already expands repeating events (AppleScript's Calendar search skips repeats, and
EventKit refuses a background process without ever asking). Its "Scheduled
Reminders" calendar carries Apple Reminders that have a time.
"""
from __future__ import annotations

import json
import math
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
OPTIONSBOTS = Path.home() / "optionsbots"
BOTS = OPTIONSBOTS / "scripts" / "bots.py"
LEARN_LATEST = OPTIONSBOTS / "data" / "learn" / "latest.json"
MEMEBOT = OPTIONSBOTS / "data" / "memebot"
HYPE = OPTIONSBOTS / "data" / "hype" / "latest.json"


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


def bots_review(path: Path = LEARN_LATEST) -> Optional[dict]:
    """The last nightly review: {"generated", "speech", "changed", ...}, or None before the first one."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def meme_trades(folder: Path = MEMEBOT) -> list[dict]:
    """Every buy and sell the memecoin bot made, oldest first (its trades.jsonl)."""
    try:
        lines = (folder / "trades.jsonl").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({})                   # keep the count of lines right for the cursor
    return out


def meme_status(folder: Path = MEMEBOT) -> Optional[dict]:
    try:
        return json.loads((folder / "status.json").read_text())
    except (OSError, ValueError):
        return None


def hype(path: Path = HYPE) -> Optional[dict]:
    """The memecoin bot's latest hourly hype reading: {"ts", "coins": {symbol: {"level", "score", "trending"}}}."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def meme_price(value) -> str:
    """Memecoins cost fractions of a cent: '$0.0000123', '$0.21', '$14.50'."""
    v = float(value)
    if v >= 1 or v <= 0:
        return f"${v:,.2f}"
    return f"${v:.{2 - math.floor(math.log10(v))}f}"               # three significant digits


# ── Gumroad ──────────────────────────────────────────────────────────────────
def gumroad_sales(after: date) -> list[dict]:
    """Real (not test) Gumroad sales since `after`: [{"id", "product", "price", "refunded"}]."""
    from plugins.product_sales import _cli_json
    data = _cli_json(["sales", "list", "--after", after.isoformat()])
    out = []
    for sale in data.get("sales") or []:
        if not isinstance(sale, dict) or not sale.get("id") or sale.get("is_test_purchase") is True \
                or sale.get("test") is True:
            continue
        out.append({"id": str(sale["id"]), "product": _short(sale.get("product_name") or "a product", 50),
                    "price": _short(sale.get("formatted_total_price"), 20), "refunded": sale.get("refunded") is True})
    return out


# ── spending and workouts, for the Sunday recap ──────────────────────────────
def money_week(today: date) -> Optional[str]:
    """This week's logged spending against the four weeks before, and any budget it broke."""
    from plugins import money_tracker as mt
    data = mt._load()
    if not data["entries"]:
        return None
    start = mt._start("week", today)
    spent = mt._spent(data["entries"], None, start)
    before = [mt._spent(data["entries"], None, start - timedelta(weeks=w))
              - mt._spent(data["entries"], None, start - timedelta(weeks=w - 1)) for w in range(1, 5)]
    line = f"Spent {mt._money(spent)} this week"
    used = [b for b in before if b]
    if used:
        avg = sum(used) / len(used)
        diff = spent - avg
        line += f", {mt._money(abs(diff))} {'more' if diff > 0 else 'less'} than usual" if abs(diff) >= 1 else ", about usual"
    over = [c for c, b in data["budgets"].items() if mt._spent(data["entries"], c, mt._start(b["period"], today)) > b["amount"]]
    if over:
        line += ". Over budget: " + ", ".join(over)
    return line + "."


def workouts_since(since: datetime) -> Optional[list[dict]]:
    """Hevy workouts started since `since`: [{"title", "day"}], or None when Hevy isn't connected."""
    from memory.config_manager import get_plugin_config
    from plugins import hevy_workouts
    key = str(get_plugin_config("hevy").get("api_key") or "").strip()
    if not key:
        return None
    out = []
    for w in hevy_workouts._fetch(key, max_pages=1):
        try:
            at = datetime.fromisoformat(str(w.get("start_time")).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        if at >= since:
            out.append({"title": _short(w.get("title") or "Workout", 30), "day": at.strftime("%a")})
    return out


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
                 fantasy_swaps: list[str], packages: list[str], overnight: list[str],
                 trading: Optional[list] = None, shop: Optional[dict] = None) -> str:
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
    for line in trading or []:
        lines.append(f"📈 {line}")
    if shop:
        sold = (f"{shop['sales']} sale{'s' if shop['sales'] != 1 else ''} in the last day"
                + (f" (${shop['revenue']:,.2f})" if shop.get("revenue") else "") if shop["sales"] else "No sales overnight")
        new = f"; new today: '{_short(shop['new'][-1], 40)}'" if shop.get("new") else ""
        lines.append(f"🛍️ Shop: {sold}{new}; {shop['listed']} designs live.")
    if overnight:
        lines.append("🌙 Overnight: " + " | ".join(overnight[:5]))
    return "\n".join(lines)


# ── the Sunday recap ─────────────────────────────────────────────────────────
def weekly_text(*, today: date, trading: Optional[str], memecoin: Optional[str], grades: list[str],
                canvas: list[dict], money: Optional[str], workouts: Optional[list[dict]], sales: Optional[int],
                events: Optional[list[dict]]) -> str:
    lines = [f"Your week, through {today.strftime('%a %b %-d')}"]
    if trading:
        lines.append(f"📈 {trading}")
    if memecoin:
        lines.append(f"🪙 {memecoin}")
    if sales:
        lines.append(f"💰 {sales} Gumroad sale{'s' if sales != 1 else ''} this week.")
    if money:
        lines.append(f"💵 {money}")
    if workouts is not None:
        lines.append(f"🏋️ {len(workouts)} workout{'s' if len(workouts) != 1 else ''}" +
                     (": " + ", ".join(f"{w['title']} ({w['day']})" for w in workouts[:5]) if workouts else "."))
    if grades:
        lines.append("🎓 Grades: " + "; ".join(grades[:5]))
    end = (today + timedelta(days=7)).isoformat()
    due = [c for c in canvas if c.get("overdue") or (today.isoformat() < (c.get("due") or "") <= end)]
    overdue = [c for c in due if c.get("overdue")]
    if overdue:
        lines.append("📚 Overdue: " + "; ".join(f"{_short(c['name'], 40)} ({_short(c['course'], 20)})" for c in overdue[:3]))
    upcoming = [c for c in due if not c.get("overdue")]
    if upcoming:
        lines.append(f"📚 Due this week ({len(upcoming)}): " + "; ".join(
            f"{_short(c['name'], 35)} ({date.fromisoformat(c['due']).strftime('%a')})" for c in upcoming[:5]))
    elif not overdue:
        lines.append("📚 Nothing due on Canvas this week.")
    timed = [e for e in events or [] if not e["all_day"]]
    if timed:
        lines.append(f"📅 Next week: {len(timed)} event{'s' if len(timed) != 1 else ''}, starting with " +
                     "; ".join(f"{e['at'].strftime('%a')} {clock(e['at'])} {e['title']}" for e in timed[:3]))
    return "\n".join(lines)
