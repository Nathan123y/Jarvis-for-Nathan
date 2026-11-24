"""Keeping the social accounts safe (11-23, after Instagram got a security lock and TikTok was buried at 0 views).

Every post to TikTok, Instagram or YouTube goes through here, whichever bot makes it:
- real_chrome(): the upload runs in a normally launched Google Chrome (its own profile per account), driven over
  Chrome's debugging port, instead of Playwright's automation Chrome with its ~30 test-only switches
  (--enable-automation, mock keychain, no extensions, swiftshader...). To the site it's an ordinary browser.
- check() before a post: the account's topic (no GTA on the stories' pages), a daily cap, a minimum gap between
  posts, no re-upload of the same file, and no posts while the account is frozen.
- a security check, suspension or "confirm you're human" page freezes that account at once (no retries into it) and
  texts the user once with what the page says; it's looked at again only every few hours.
- browse_a_bit(): a short scroll of the feed before an upload, so a session isn't just "open, upload, leave".
- jittered(): the day's post times move by up to 20 minutes, so posts don't land at the same minute every day.
"""
from __future__ import annotations

import hashlib
import json
import random
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE = ROOT / "memory" / "social_gate.json"
NS = "social_gate"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULTS = {"real_chrome": True,
            # each account's one topic, posts a day and minutes between posts
            "accounts": {"tiktok": {"niche": "stories", "per_day": 2, "gap_min": 180},
                         "instagram": {"niche": "stories", "per_day": 2, "gap_min": 180},
                         "youtube": {"niche": "gta", "per_day": 16, "gap_min": 20}},
            "recheck_h": 6,                 # a frozen account is looked at again after this long
            "browse": True}
NAMES = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}


class Blocked(RuntimeError):
    """The gate said no to this post (cap, gap, topic, duplicate or frozen account). Nothing was sent."""


class AccountLocked(RuntimeError):
    """The site showed a security check / suspension. The account is now frozen."""


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in stored.items():
        if k == "accounts" and isinstance(v, dict):
            for plat, rules in v.items():
                out["accounts"].setdefault(plat, {}).update(rules or {})
        elif k in DEFAULTS and v is not None:
            out[k] = v
    return out


def _load(path: Optional[Path] = None) -> dict:
    try:
        st = json.loads((path or STATE).read_text())
    except Exception:
        st = {}
    for k in ("posts", "frozen", "uploads"):
        st.setdefault(k, {})
    return st


def _save(st: dict, path: Optional[Path] = None) -> None:
    path = path or STATE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def fingerprint(video) -> str:
    """Size + the first 4 MB: the same file uploaded twice is caught without reading whole videos."""
    p = Path(video)
    try:
        with p.open("rb") as f:
            head = f.read(4 << 20)
        return hashlib.sha1(head + str(p.stat().st_size).encode()).hexdigest()[:20]
    except OSError:
        return ""


# ── the gate ─────────────────────────────────────────────────────────────────
def check(platform: str, niche: Optional[str] = None, video=None, now: Optional[float] = None,
          cfg: Optional[dict] = None, path: Optional[Path] = None) -> None:
    """Raises Blocked when this post mustn't go out now. niche=None skips the topic rule."""
    now = now or time.time()
    cfg = cfg or settings()
    rules = cfg["accounts"].get(platform) or {}
    st = _load(path)
    fz = st["frozen"].get(platform)
    if fz and now - fz.get("at", 0) < float(cfg.get("recheck_h") or 6) * 3600:
        raise Blocked(f"{NAMES.get(platform, platform)} is frozen: {fz.get('why', '')[:120]}")
    if niche and rules.get("niche") and niche != rules["niche"]:
        raise Blocked(f"{NAMES.get(platform, platform)} is for {rules['niche']} only, not {niche}")
    day = datetime.fromtimestamp(now).date()
    times = [t for t in st["posts"].get(platform, []) if datetime.fromtimestamp(t).date() == day]
    if rules.get("per_day") and len(times) >= int(rules["per_day"]):
        raise Blocked(f"{NAMES.get(platform, platform)} already had {len(times)} posts today (cap {rules['per_day']})")
    last = max(st["posts"].get(platform, []) or [0])
    gap = float(rules.get("gap_min") or 0) * 60
    if gap and now - last < gap:
        raise Blocked(f"{NAMES.get(platform, platform)} posted {int((now - last) / 60)} min ago (gap {int(gap / 60)} min)")
    fp = fingerprint(video) if video else ""
    if fp and fp in st["uploads"].get(platform, []):
        raise Blocked(f"this exact video is already on {NAMES.get(platform, platform)}")


def record(platform: str, video=None, now: Optional[float] = None, path: Optional[Path] = None) -> None:
    """A post went up: count it, remember the file, and lift any freeze (the account is clearly fine)."""
    now = now or time.time()
    st = _load(path)
    st["posts"][platform] = [t for t in st["posts"].get(platform, []) if now - t < 14 * 86400] + [now]
    fp = fingerprint(video) if video else ""
    if fp:
        st["uploads"][platform] = (st["uploads"].get(platform, []) + [fp])[-500:]
    st["frozen"].pop(platform, None)
    _save(st, path)


def freeze(platform: str, why: str, log: Callable = print, path: Optional[Path] = None, text: Callable = None) -> None:
    """Stop posting to this account and tell the user once a day at most."""
    st = _load(path)
    old = st["frozen"].get(platform) or {}
    told = old.get("told", 0)
    st["frozen"][platform] = {"why": why[:300], "at": time.time(), "told": told}
    msg = ""
    if time.time() - told > 86400:
        st["frozen"][platform]["told"] = time.time()
        msg = (f"🔒 {NAMES.get(platform, platform)} needs you: \"{why[:200]}\". I stopped posting there so it doesn't "
               f"get worse. Its Chrome window is on your screen: clear it there or in the app (usually a texted code). I'll check again "
               f"in a few hours and pick back up on my own.")
    _save(st, path)
    log(f"gate: froze {platform}: {why[:160]}")
    if msg:
        try:
            if text is None:
                from core import phone_alerts
                text = phone_alerts.send_text
            text(msg)
        except Exception as exc:
            log(f"gate: couldn't text about the freeze: {exc}")


def unfreeze(platform: str, path: Optional[Path] = None) -> None:
    st = _load(path)
    st["frozen"].pop(platform, None)
    _save(st, path)


LOCK_URLS = ("/accounts/suspended", "/challenge", "/checkpoint", "/accounts/disabled", "accounts.google.com/v3/signin/challenge",
             "/signin/rejected")
LOCK_TEXT = ("confirm you're human", "enter confirmation code", "we suspended your account", "your account has been disabled",
             "your account was banned", "account suspended", "unusual activity", "verify to continue", "verify it's you",
             "help us confirm", "automated behavior", "suspicious activity")


def lock_reason(url: str, text: str) -> str:
    """What a security check / suspension page says, or "" for a normal page."""
    low = " ".join(str(text or "").lower().split())
    for u in LOCK_URLS:
        if u in str(url or ""):
            hit = next((t for t in LOCK_TEXT if t in low), "")
            return (hit or u.strip("/")).capitalize() + " page"
    for t in LOCK_TEXT:
        if t in low:
            return t.capitalize()
    return ""


def checkpoint_reason(pg) -> str:
    try:
        text = pg.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''") or ""
        if pg.locator("#captcha-verify-container, [id*='captcha'], iframe[src*='captcha']").count():
            return "Captcha (\"verify to continue\")"
        return lock_reason(pg.url, text)
    except Exception:
        return ""


def on_failure(pg, platform: str, log: Callable = print) -> None:
    """Called from an upload's error path: a lock page freezes the account and becomes AccountLocked. The window
    comes onto the screen so the user can type the code straight in."""
    why = checkpoint_reason(pg)
    if why:
        freeze(platform, why, log)
        place_window(pg, on_screen=True)
        raise AccountLocked(f"{NAMES.get(platform, platform)} showed a security page: {why}")


# ── a normal Chrome ──────────────────────────────────────────────────────────
class _RealContext:
    """Playwright's BrowserContext for one of the always-open account Chromes. close() only lets go of it: the
    Chrome stays open (logins stay warm and the user can see it), extra tabs are shut, and a window brought on
    screen for a sign-in goes back to its parking spot."""

    def __init__(self, browser, profile: Path, park_on_close: bool):
        self._browser, self._profile, self._park = browser, profile, park_on_close
        self._ctx = browser.contexts[0]

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def close(self) -> None:
        try:
            pages = self._ctx.pages
            for extra in pages[1:]:
                extra.close()
            if self._park and pages:
                place_window(pages[0], on_screen=False)
        except Exception:
            pass
        try:
            self._browser.close()               # connected over CDP: this only disconnects
        except Exception:
            pass


PROFILES = {"tiktok": ROOT / "memory" / "tiktok_profile", "instagram": ROOT / "memory" / "instagram_profile",
            "youtube": ROOT / "memory" / "youtube_profile"}
PORTS = {"tiktok_profile": 9301, "instagram_profile": 9302, "youtube_profile": 9303}
HOMES = {"tiktok": "https://www.tiktok.com/foryou", "instagram": "https://www.instagram.com/",
         "youtube": "https://studio.youtube.com/"}


def port_for(profile: Path) -> int:
    name = Path(profile).name
    return PORTS.get(name) or 9310 + int(hashlib.md5(name.encode()).hexdigest(), 16) % 80


def platform_of(profile: Path) -> str:
    return next((k for k, v in PROFILES.items() if Path(v).name == Path(profile).name), "")


def _commands() -> list[str]:
    try:
        return subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except Exception:
        return []


def _alive(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def running(profile: Path) -> str:
    """"ours" = our Chrome is up on its port, "other" = something else holds the profile, "" = nothing."""
    flag, port = f"--user-data-dir={profile} ", port_for(profile)       # the space: not tiktok_profile_old_...
    mine = [c for c in _commands() if flag in c + " " and "--type=" not in c]
    if any(f"--remote-debugging-port={port}" in c for c in mine) and _alive(port):
        return "ours"
    return "other" if mine else ""


def _quit_leftover(profile: Path) -> None:
    """A Chrome on this profile that we can't drive (an old-style window, or one that hung) would swallow the launch."""
    subprocess.run(["pkill", "-f", f"--user-data-dir={profile}( |$)"], capture_output=True)
    time.sleep(2)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile / name).unlink()
        except OSError:
            pass


def launch(profile: Path, size=(1280, 900), home: str = "about:blank") -> None:
    """Start the account's Chrome, parked off screen, in its own session so a worker restart doesn't take it down."""
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    port = port_for(profile)
    subprocess.Popen([CHROME, f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "--no-first-run",
                      "--no-default-browser-check", f"--window-size={size[0]},{size[1] + 120}",
                      "--window-position=-2400,0", home],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 60
    while time.time() < end:
        if _alive(port):
            return
        time.sleep(0.5)
    raise RuntimeError("Chrome didn't open its debugging port")


def ensure(profile: Path, size=(1280, 900), take_over: bool = True) -> bool:
    """The account's Chrome is up. take_over=False leaves a profile alone while something else is using it."""
    state = running(profile)
    if state == "ours":
        return True
    if state == "other":
        if not take_over:
            return False
        _quit_leftover(profile)
    launch(profile, size, HOMES.get(platform_of(profile), "about:blank"))
    return True


def _ensure_window(port: int, home: str) -> None:
    """On a Mac, closing a Chrome's last window leaves it running with no window at all, and Playwright can't attach
    to that ("Browser context management is not supported"). Open a fresh window when none is left."""
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
        if not any(t.get("type") == "page" for t in tabs):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{home}", method="PUT")
            urllib.request.urlopen(req, timeout=10).read()
            time.sleep(2)
    except Exception:
        pass


def place_window(pg, on_screen: bool) -> None:
    """Bring the Chrome window onto the screen (in front) or park it off to the side."""
    try:
        cdp = pg.context.new_cdp_session(pg)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": 120 if on_screen else -2400, "top": 80}})
        if on_screen:
            pg.bring_to_front()
    except Exception:
        pass


def frozen(platform: str) -> bool:
    return bool(platform and _load()["frozen"].get(platform))


def real_chrome(p, profile: Path, visible: bool, size=(1280, 900)):
    """Connect to the account's always-open Chrome (starting it if needed). visible = put it on screen for the
    user (a sign-in), and park it again afterwards. A frozen account's window stays on screen so the code can go in."""
    profile = Path(profile)
    ensure(profile, size)
    _ensure_window(port_for(profile), HOMES.get(platform_of(profile), "about:blank"))
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port_for(profile)}", timeout=60000)
    ctx = _RealContext(browser, profile, park_on_close=visible)
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    if visible:
        place_window(pg, on_screen=True)
    elif not frozen(platform_of(profile)):
        place_window(pg, on_screen=False)
    return ctx


class KeeperService:
    """Keeps every account's Chrome open (it may have crashed, or Chrome updated and quit). Never takes a profile
    away from something else using it."""
    PASS_SECONDS = 600

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self._stop = __import__("threading").Event()

    def start(self) -> None:
        __import__("threading").Thread(target=self._run, daemon=True, name="account-chromes").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            self.run_once()
            if self._stop.wait(self.PASS_SECONDS):
                return

    def run_once(self) -> None:
        if not settings().get("real_chrome", True):
            return
        for plat, profile in PROFILES.items():
            try:
                if running(profile) != "ours" and ensure(profile, take_over=False):
                    self.log(f"gate: opened the {NAMES[plat]} Chrome")
            except Exception as exc:
                self.log(f"gate: couldn't open the {NAMES[plat]} Chrome: {exc}")


def open_profile(p, profile: Path, visible: bool, size=(1280, 900), legacy: Optional[Callable] = None):
    """real_chrome() when it's on (the default), else / if it fails, the old automation launch `legacy`."""
    if settings().get("real_chrome", True) or legacy is None:
        try:
            return real_chrome(p, profile, visible, size)
        except Exception:
            if legacy is None:
                raise
    return legacy()


# ── looking like a person ────────────────────────────────────────────────────
def browse_a_bit(pg, url: str = "", seconds: tuple = (15, 40), log: Callable = print) -> None:
    """Scroll a feed for a little while, at a reading pace. Never likes, follows or comments."""
    if not settings().get("browse", True):
        return
    try:
        if url:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        end = time.time() + random.uniform(*seconds)
        pg.wait_for_timeout(random.randint(2500, 5000))
        while time.time() < end:
            pg.mouse.wheel(0, random.randint(400, 1100))
            pg.wait_for_timeout(random.randint(2500, 7000))
    except Exception as exc:
        log(f"gate: browsing before the post didn't work ({str(exc)[:80]}); posting anyway")


def jittered(times: str, day: str, minutes: int = 20) -> str:
    """The day's "HH:MM" list, each moved by a steady per-day amount in [-minutes, +minutes]."""
    out = []
    for s in (x.strip() for x in str(times).split(",") if x.strip()):
        try:
            h, m = (int(x) for x in s.split(":"))
        except ValueError:
            continue
        off = int(hashlib.md5(f"{day} {s}".encode()).hexdigest(), 16) % (2 * minutes + 1) - minutes
        t = max(0, min(23 * 60 + 59, h * 60 + m + off))
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    return ",".join(out)


def status() -> str:
    st, cfg = _load(), settings()
    lines = []
    for plat, rules in cfg["accounts"].items():
        today = [t for t in st["posts"].get(plat, []) if datetime.fromtimestamp(t).date() == datetime.now().date()]
        fz = st["frozen"].get(plat)
        lines.append(f"{NAMES.get(plat, plat)}: {rules.get('niche')} only, {len(today)}/{rules.get('per_day')} today"
                     + (f", FROZEN ({fz['why'][:80]})" if fz else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "unfreeze":          # python3 -m worker.social_gate unfreeze instagram
        unfreeze(sys.argv[2])
    print(status())
