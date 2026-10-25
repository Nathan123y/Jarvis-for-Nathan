"""Text the user's own phone when something important happens while they're away.

The background worker (worker/alerts.py) finds the news: a Jarvis reminder that
just came due, an important unread email, a website customer who wants a call or
paid, a background job that broke, a big paper-trading loss, Canvas work due
within a few hours, a Canvas grade, a memecoin bot trade, a Gumroad sale. Everything goes through notify() here, which decides whether
it becomes a text:

  * only while the user is away from the Mac (screen locked, or no keyboard or
    mouse for `away_minutes`); at the Mac, Jarvis's own voice and briefings cover it;
  * each kind can be switched off ("stop texting me about Canvas");
  * during quiet hours everything but reminders is held and sent as one text when
    they end; past `max_per_hour` texts, the rest are held the same way.

Texts go by iMessage to the user's own number (Contacts "my card", or the number
in settings) and start with "JARVIS:" so they stand out in the thread.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.events import Store

NS = "phone_alerts"
KINDS = {
    "reminders": "reminders",
    "email": "important emails",
    "vip": "emails from the people you picked",
    "calendar": "calendar events starting soon",
    "business": "the website business",
    "trading": "big trading losses",
    "trading_daily": "the daily trading summary",
    "bots_review": "the bots' nightly review",
    "memecoin": "memecoin bot trades and big moves",
    "sales": "Gumroad sales",
    "shop": "Etsy shop listings and orders",
    "internships": "internship applications and company replies",
    "videos": "the daily shop videos (sent to you to post until Instagram posts them itself)",
    "canvas": "Canvas work due soon",
    "canvas_tonight": "the 8 PM 'due tonight' check",
    "grades": "Canvas grades posted",
    "fantasy": "fantasy football injuries and lineup problems",
    "packages": "packages",
    "morning": "the morning text",
    "weekly": "the Sunday recap",
    "system": "Jarvis problems",
}
ALWAYS = ("memecoin",)       # the 24/7 memecoin bot: every trade and profit step is texted, at the Mac or not, day or night
SCHEDULED = ("morning", "trading_daily", "canvas_tonight", "bots_review", "weekly")   # summaries you asked for: sent at the Mac too
DEFAULTS = {
    "enabled": True,
    "my_number": "",
    "away_minutes": 5,
    "quiet_start": 23,        # hour, local time; quiet_start == quiet_end turns quiet hours off
    "quiet_end": 7,
    "max_per_hour": 8,
    "loss_alert": 500,        # dollars lost on one paper trade
    "canvas_hours": 3,        # warn this long before something is due
    "off": [],                # kinds switched off
    "vip": [],                # email addresses, domains or names that always get a text
    "city": "San Jose",       # for the morning weather
    "morning_time": "07:00",
    "trading_time": "13:30",  # weekdays, after the 1:00 PM Pacific close
    "calendar_minutes": 15,
    "tonight_time": "20:00",  # "due tonight" check
    "weekly_time": "18:00",   # Sundays
    "meme_move": 20,
    "meme_profit_step": 25,   # dollars: a text each time the memecoin account's total profit (or loss) crosses another step          # percent a memecoin position moves before a text
}
PREFIX = "JARVIS: "
_STATE = "phone_alerts_state"
TEST_FLAG = "phone_alerts_test"
NOW_FLAG = "phone_alerts_now"        # {"kind": "morning" | "trading_daily" | "fantasy" | "weekly", "at": time}: send that summary
_MAX_TEXT = 1200


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = stored.get(key)
        if value in (None, ""):
            continue
        try:
            if isinstance(default, bool):
                out[key] = value if isinstance(value, bool) else str(value).strip().lower() in ("on", "true", "yes", "1")
            elif isinstance(default, int):
                out[key] = int(float(value))
            elif isinstance(default, list):
                out[key] = [str(v) for v in value] if isinstance(value, list) else \
                    [v.strip() for v in str(value).split(",") if v.strip()]
            else:
                out[key] = str(value).strip()
        except (TypeError, ValueError):
            pass
    return out


def is_away(minutes: float, screen: Optional[str] = None, idle: Optional[float] = None) -> bool:
    """Locked (or unknown, e.g. lid closed at the login window) or idle long enough."""
    from core import away
    screen = screen if screen is not None else away.screen_state()
    if screen != "unlocked":
        return True
    idle = idle if idle is not None else away.idle_seconds()
    return idle is not None and idle >= minutes * 60


def in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    if start == end:
        return False
    h = now.hour
    return start <= h < end if start < end else (h >= start or h < end)


def my_handle(cfg: Optional[dict] = None) -> str:
    """The number to text: the one in settings, else the first phone on the Contacts 'my card'."""
    cfg = cfg or settings()
    if cfg.get("my_number"):
        from core.macos_communications import normalize_phone
        return normalize_phone(cfg["my_number"])
    import subprocess
    out = subprocess.run(
        ["osascript", "-e", 'tell application "Contacts" to get value of first phone of my card'],
        capture_output=True, text=True, timeout=15)
    number = out.stdout.strip()
    if out.returncode != 0 or not number:
        raise RuntimeError("I couldn't find your number. Add it under Phone alerts in Plugin Settings.")
    from core.macos_communications import normalize_phone
    return normalize_phone(number)


def send_text(text: str, cfg: Optional[dict] = None) -> None:
    from core.macos_communications import Contact, send_message
    send_message(Contact("Me", my_handle(cfg)), (PREFIX + text)[:_MAX_TEXT])


_FILE_SCRIPT = '''
on run argv
    set theFile to POSIX file (item 2 of argv)
    tell application "Messages"
        set usableServices to every service whose service type is iMessage
        if (count of usableServices) is 0 then error "No iMessage service is signed in."
        set targetBuddy to buddy (item 1 of argv) of (item 1 of usableServices)
        if (item 3 of argv) is not "" then send (item 3 of argv) to targetBuddy
        send theFile to targetBuddy
    end tell
    return "OK"
end run
'''
FILE_DIR = Path.home() / "Pictures" / "Jarvis Videos"   # Messages is sandboxed: it only sends files from places like this


def send_file(path, note: str = "", cfg: Optional[dict] = None) -> None:
    """Text a file (a video) to the user's own phone, with an optional note before it."""
    import shutil
    import subprocess
    src = Path(path)
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    dest = FILE_DIR / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    out = subprocess.run(["osascript", "-", my_handle(cfg), str(dest), (PREFIX + note) if note else ""],
                         input=_FILE_SCRIPT, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "Messages couldn't send the file").strip()[:200])


class Alerts:
    """notify()/flush() with the send, the away check and the clock injectable for tests."""

    def __init__(self, store: Store, *, send: Callable[[str], None] = None,
                 away: Callable[[float], bool] = None, clock: Callable[[], float] = time.time,
                 cfg: Callable[[], dict] = settings, log: Callable[[str], None] = lambda m: None):
        self.store, self.clock, self.cfg, self.log = store, clock, cfg, log
        self._send = send or (lambda text: send_text(text))
        self._away = away or is_away

    def _state(self) -> dict:
        st = self.store.get(_STATE) or {}
        return {"sent": list(st.get("sent") or []), "held": list(st.get("held") or [])}

    def _save(self, st: dict) -> None:
        self.store.set(_STATE, st)

    def _deliver(self, st: dict, text: str, now: float) -> bool:
        try:
            self._send(text)
        except Exception as exc:
            self.log(f"phone alert failed: {exc!r}")
            return False
        st["sent"] = [t for t in st["sent"] if now - t < 3600] + [now]
        self.log(f"phone alert sent: {text[:80]}")
        return True

    def notify(self, kind: str, text: str) -> str:
        """'sent', 'held', 'present', 'off' or 'failed'."""
        cfg, now = self.cfg(), self.clock()
        if not cfg["enabled"] or kind in cfg["off"]:
            return "off"
        always = kind in ALWAYS
        if not always and not self._away(cfg["away_minutes"]):
            return "present"
        st = self._state()
        recent = [t for t in st["sent"] if now - t < 3600]
        quiet = kind != "reminders" and not always and in_quiet_hours(datetime.fromtimestamp(now), cfg["quiet_start"], cfg["quiet_end"])
        if quiet or len(recent) >= cfg["max_per_hour"]:
            st["held"] = (st["held"] + [text])[-20:]
            self._save(st)
            return "held"
        ok = self._deliver(st, text, now)
        self._save(st)
        return "sent" if ok else "failed"

    def take_held(self) -> list[str]:
        st = self._state()
        held, st["held"] = st["held"], []
        self._save(st)
        return held

    def scheduled(self, kind: str, text: str, force: bool = False) -> str:
        """A summary the user asked for at a set time: sent whether or not they're at the Mac.
        `force` is "send it now" by voice, which goes out even when that kind is muted."""
        cfg = self.cfg()
        if not force and (not cfg["enabled"] or kind in cfg["off"]):
            return "off"
        st = self._state()
        ok = self._deliver(st, text, self.clock())
        self._save(st)
        return "sent" if ok else "failed"

    def test_if_asked(self) -> str:
        """Send the test text the user asked for, at the Mac or not."""
        if not self.store.get(TEST_FLAG):
            return ""
        self.store.set(TEST_FLAG, 0)
        try:
            self._send("Test text. Phone alerts work; I'll text you here when something "
                       "important comes up while you're away.")
            return "sent"
        except Exception as exc:
            self.log(f"phone alert test failed: {exc!r}")
            return "failed"

    def flush(self) -> str:
        """Send what was held as one text, once quiet hours are over and texting is allowed again."""
        cfg, now = self.cfg(), self.clock()
        st = self._state()
        if not st["held"]:
            return "empty"
        if not cfg["enabled"]:
            st["held"] = []
            self._save(st)
            return "off"
        if in_quiet_hours(datetime.fromtimestamp(now), cfg["quiet_start"], cfg["quiet_end"]):
            return "quiet"
        if len([t for t in st["sent"] if now - t < 3600]) >= cfg["max_per_hour"]:
            return "limited"
        if not self._away(cfg["away_minutes"]):
            st["held"] = []                        # back at the Mac: the briefing covers it
            self._save(st)
            return "present"
        held = st["held"]
        body = held[0] if len(held) == 1 else \
            f"{len(held)} things while you were away:\n" + "\n".join(f"• {h}" for h in held)
        if self._deliver(st, body, now):
            st["held"] = []
        self._save(st)
        return "sent"
