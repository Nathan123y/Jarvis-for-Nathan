"""Home: the one dashboard, in two tabs. Portfolio (worker/home_portfolio.py, worker/home_chart.py) shows the paper trading
accounts like a brokerage app: value over time, accounts, open positions, a candle chart with the bots' buys and sells, the
trade history and each strategy's record. Operations shows what needs a look, shop sales and one line per crew (click a crew
for its agents). The detailed trading engine view stays at http://localhost:8444.

    http://localhost:8460          (the worker serves it; nothing to install, nothing leaves this Mac)

Each crew is one of the worker's services (the shop, the daily videos, game clips, game guides, internships, alerts) plus the
memecoin crew that lives in ~/optionsbots. A crew's agents are its roles. For each role the page shows when it last did
something and what it said, read from the worker's log, plus a few numbers from each crew's own state file. The page asks
for fresh data every 10 seconds.

An agent's light: green = active in the last 15 minutes, blue = active in the last 24 hours, grey = quiet. A last message
that reports a failure is shown in amber.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "config" / "worker" / "worker.log"
PORT = 8460
TAIL_BYTES = 900_000                      # the log's last stretch is plenty for "what did each agent do last"
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) (jarvis\.[\w.]+) (.*)$")
PROBLEM = re.compile(r"fail|couldn't|could not|error|refused|flaw|timed out|wrong|not logged in|isn't logged in|❌", re.I)

# crew -> (logger prefix, [(agent, what it does, pattern on the log message[, a different logger prefix to look in])])
CREWS = [
    ("Shop crew", "jarvis.shop", [
        ("Trend Scout", "reads what is going viral and picks the jokes", r"trend|scout|memes"),
        ("Designer", "turns an idea into a drawing and a listing", r"designed '|drawing \d|idea refused|today's design"),
        ("Publisher", "lists each design on Etsy, eBay, the Pop-Up Store and TikTok Shop (its texts come from the alerts log)",
         r"Just listed|listing", "jarvis.alerts"),
        ("Art repair", "redraws old listings that look bad", r"redrew|redraw"),
        ("Storefront", "keeps the website and Pinterest feed up to date", r"storefront"),
        ("Analyst", "reviews results every evening and rewrites the notebook", r"Analyst|notebook"),
    ]),
    ("Video crew", "jarvis.viral", [
        ("Scriptwriter", "writes each 'what if' video from the topic queue", r"script|topic|idea|writing"),
        ("Builder", "renders the 3D scenes, voice and sound", r"render|mixing|scene|frames"),
        ("Quality checker", "watches and listens before anything is posted", r"quality check|sound check|sound fix"),
        ("TikTok poster", "posts to TikTok", r"TikTok|tiktok"),
        ("Instagram poster", "posts the Reel", r"Instagram"),
    ]),
    ("Game clips crew", "jarvis.clip", [
        ("Clip scout and cutter", "finds gameplay clips, cuts them vertical and posts them", r"."),
    ]),
    ("Game guides crew", "jarvis.guides", [
        ("Guide maker", "writes, narrates, edits and uploads the guide videos", r"."),
    ]),
    ("Internship crew", "jarvis.internships", [
        ("Applier", "applies to Summer 2027 internships", r"appl|submit"),
        ("Inbox reader", "reads company replies", r"repl|inbox|email"),
        ("Scout", "looks for new openings", r"\bscout\b|director'?s? sheet"),   # not "ServerNotFoundError"
    ]),
    ("Phone and alerts", "jarvis.alerts", [
        ("Texter", "sends you the important news and sale alerts", r"."),
    ]),
    ("Worker", "jarvis.worker", [
        ("Supervisor", "keeps all of the above running", r"."),
    ]),
]


def read_log(path: Path = LOG, limit: int = TAIL_BYTES) -> list:
    """[(datetime, level, logger, message)] from the end of the log, oldest first. Lines that continue a message
    (tracebacks) are skipped."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        m = LINE.match(line)
        if m:
            try:
                out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2), m.group(3), m.group(4)))
            except ValueError:
                pass
    return out


def light(age_s: Optional[float]) -> str:
    if age_s is None:
        return "grey"
    return "green" if age_s <= 900 else "blue" if age_s <= 86400 else "grey"


def plain(msg: str) -> str:
    """A log message without the programmer noise: an exception's long details become just its name, no file paths or
    escaped newlines."""
    msg = re.sub(r"\b(\w+(?:Error|Exception|Timeout))\((.*)$", r"\1", msg)
    msg = re.sub(r"(\\n|\n).*$", "", msg)
    msg = re.sub(r"(/Users|/private|/Library)/\S+", "", msg)
    msg = msg.replace("TimeoutError", "timed out")
    return re.sub(r"\s{2,}", " ", msg).strip(" :-")


def agents_status(lines: list, now: Optional[datetime] = None) -> list:
    """The crews with each agent's last activity. [{crew, agents: [{name, role, light, age_s, last, problem}]}]"""
    now = now or datetime.now()
    out = []
    for crew, prefix, agents in CREWS:
        rows = []
        for name, role, pattern, *own in agents:
            where = own[0] if own else prefix
            mine = [l for l in lines if l[2].startswith(where)]
            rx = re.compile(pattern, re.I)
            hit = next((l for l in reversed(mine) if rx.search(l[3])), None)
            if hit:
                age = max(0.0, (now - hit[0]).total_seconds())
                msg = plain(re.sub(r"^\w+: ", "", hit[3]))[:220]
                rows.append({"name": name, "role": role, "light": light(age), "age_s": age, "last": msg,
                             "problem": hit[1] in ("ERROR", "WARNING") or bool(PROBLEM.search(hit[3]))})
            else:
                rows.append({"name": name, "role": role, "light": "grey", "age_s": None, "last": "nothing in the recent log",
                             "problem": False})
        out.append({"crew": crew, "agents": rows})
    return out


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def numbers(now: Optional[datetime] = None) -> list:
    """[{crew, items: [[label, value]]}]: a few facts from each crew's own state file."""
    now = now or datetime.now()
    today = now.date()
    out = []
    shop = _json(ROOT / "memory" / "etsy_shop.json") or {}
    designs = shop.get("designs") or []
    chan = {c: sum(len((d.get("products") if c == "etsy" else (d.get("channels") or {}).get(c)) or {}) for d in designs)
            for c in ("etsy", "ebay", "popup", "tiktok")}
    made_today = sum(1 for d in designs if d.get("made") and datetime.fromtimestamp(d["made"]).date() == today)
    out.append({"crew": "Shop crew", "items": [["designs made", len(designs)], ["made today", made_today],
                                              ["products sent to TikTok Shop", chan["tiktok"]], ["on the Pop-Up Store", chan["popup"]],
                                              ["on eBay", chan["ebay"]], ["on Etsy", chan["etsy"]],
                                              ["sales so far", len(shop.get("sales_log") or [])]]})
    viral = _json(ROOT / "memory" / "viral.json") or {}
    vids = viral.get("videos") or []
    posted_today = sum(1 for v in vids if v.get("made") and datetime.fromtimestamp(v["made"]).date() == today)
    out.append({"crew": "Video crew", "items": [["videos made", len(vids)], ["made today", posted_today],
                                               ["topics queued", len(viral.get("topic_queue") or [])],
                                               ["held for review", len(viral.get("held") or [])]]})
    inbox = Path.home() / "Movies" / "Jarvis Clips" / "inbox"
    try:
        waiting = sum(1 for p in inbox.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"))
    except OSError:
        waiting = 0
    out.append({"crew": "Game clips crew", "items": [["clips waiting to be cut", waiting]]})
    guides = Path.home() / "Movies" / "Jarvis Guides"
    try:
        made = sum(1 for p in guides.rglob("*.mp4"))
    except OSError:
        made = 0
    out.append({"crew": "Game guides crew", "items": [["guide videos on disk", made]]})
    intern = _json(ROOT / "memory" / "internships.json") or {}
    out.append({"crew": "Internship crew", "items": [["jobs tracked", len(intern.get("jobs") or {})],
                                                    ["replies seen", len(intern.get("replies_seen") or [])]]})
    return out


def memecoin_feed(limit: int = 6) -> list:
    """The memecoin crew's latest messages to each other (it lives in ~/optionsbots)."""
    data = _json(Path.home() / "optionsbots" / "data" / "memebot" / "agents.json") or {}
    feed = data.get("feed") or []
    return [{"ts": f.get("ts", ""), "from": f.get("from", ""), "to": f.get("to", ""), "text": str(f.get("text", ""))[:200]}
            for f in feed[-limit:]][::-1]


# ---- money: the paper trading account (~/optionsbots), the memecoin bot and the shop's sales ----

OB = Path.home() / "optionsbots"
OB_DB = OB / "data" / "native" / "paper.db"
OB_HEALTH = "http://localhost:8444/api/health"
NY = ZoneInfo("America/New_York")
KINDS = {"drive": "open-drive", "orb-scalp": "opening-range scalp", "orb-day": "opening-range day"}


def bot_label(bot_id: str) -> str:
    """'qqq-orb-scalp' -> 'QQQ opening-range scalp'"""
    sym, _, kind = bot_id.partition("-")
    return f"{sym.upper()} {KINDS.get(kind, kind)}".strip()


def _ny(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(NY)


from worker.home_portfolio import round_trips  # noqa: E402  (shared with the Portfolio tab)


def _engine_up(url: str = OB_HEALTH) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read()).get("engine") == "UP"
    except Exception:
        return False


def trading(now: Optional[datetime] = None, db: Path = OB_DB) -> dict:
    """The paper options account: balance, today, this week by day, each bot's record and the latest trades."""
    now = now.astimezone(NY) if now and now.tzinfo else datetime.now(NY)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = con.execute("select bot_id, side, contract_id, contract_json, quantity, price, ts from fills order by seq").fetchall()
        snap = con.execute("select payload_json, ts from events where type='ACCOUNT_SNAPSHOT' order by seq desc limit 1").fetchone()
        con.close()
    except sqlite3.Error:
        return {"ok": False}
    s = json.loads(snap[0]) if snap else {}
    snap_today = bool(snap) and _ny(snap[1]).date() == now.date()
    trades = round_trips(rows)
    equity = float(s.get("equity") or 0)
    start = float(s.get("starting_cash") or 0)
    open_pnl = float(s.get("unrealized") or 0) if snap_today else 0.0
    today = sum(t["pnl"] for t in trades if t["closed"].date() == now.date()) + open_pnl
    monday = now.date() - timedelta(days=now.weekday())
    days = []
    for i in range(5):
        d = monday + timedelta(days=i)
        mine = [t for t in trades if t["closed"].date() == d]
        days.append({"day": d.strftime("%a"), "date": d.isoformat(), "pnl": round(sum(t["pnl"] for t in mine)),
                     "trades": len(mine), "future": d > now.date()})
    bots = {}
    for t in trades:
        b = bots.setdefault(t["bot"], {"bot": t["bot"], "name": bot_label(t["bot"]), "trades": 0, "wins": 0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += t["pnl"] > 0
        b["pnl"] += t["pnl"]
    for bid in (s.get("per_bot") or {}):
        bots.setdefault(bid, {"bot": bid, "name": bot_label(bid), "trades": 0, "wins": 0, "pnl": 0.0})
    drive = [t for t in trades if t["bot"].endswith("-drive")]
    recent = [{"name": bot_label(t["bot"]), "what": f'{t["qty"]}x {t["symbol"]} {t["kind"]}', "pct": t["pct"], "pnl": round(t["pnl"]),
               "when": t["closed"].astimezone().strftime("%a %-I:%M %p")} for t in trades[-6:]][::-1]
    return {"ok": True, "equity": round(equity), "start": round(start), "since_start": round(equity - start),
            "today": round(today), "week": round(sum(d["pnl"] for d in days)), "days": days,
            "open_positions": len(s.get("positions") or []) if snap_today else 0, "reconciled": s.get("reconciled", True),
            "bots": sorted(({**b, "pnl": round(b["pnl"])} for b in bots.values()), key=lambda b: -b["pnl"]),
            "recent": recent, "drive": {"trades": len(drive), "wins": sum(t["pnl"] > 0 for t in drive),
                                        "avg_pct": round(sum(t["pct"] for t in drive) / len(drive), 1) if drive else 0.0},
            "engine_up": _engine_up()}


def memecoin(path: Path = OB / "data" / "memebot" / "status.json") -> dict:
    s = _json(path) or {}
    if not s:
        return {"ok": False}
    eq, first = float(s.get("equity") or 0), float(s.get("first_equity") or 0)
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(s["heartbeat"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = None
    notes = s.get("notes") or []
    return {"ok": True, "equity": round(eq), "since_start": round(eq - first), "today": round(eq - float(s.get("day_start_equity") or eq)),
            "holding": [p.get("symbol", "?") for p in s.get("positions") or [] if isinstance(p, dict)],
            "note": re.sub(r"^\d\d:\d\d ", "", notes[-1]) if notes else "", "age_s": age}


def shop_money(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    sales = (_json(ROOT / "memory" / "etsy_shop.json") or {}).get("sales_log") or []
    week = [x for x in sales if x.get("at") and (now - datetime.fromtimestamp(x["at"])).days < 7]
    return {"sales": len(sales), "total": round(sum(float(x.get("price") or 0) for x in sales), 2),
            "week": len(week), "week_total": round(sum(float(x.get("price") or 0) for x in week), 2)}


def _battery() -> Optional[dict]:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)%", out)
    return {"pct": int(m.group(1)), "plugged": "AC Power" in out} if m else None


def attention(crews: list, trade: dict, coin: dict, batt: Optional[dict], now: Optional[datetime] = None) -> list:
    """Short plain sentences for the 'Needs a look' box, most important first."""
    now_ny = datetime.now(NY)
    out = []
    market_day = now_ny.weekday() < 5 and (9, 30) <= (now_ny.hour, now_ny.minute) < (16, 0)   # New York market hours
    if trade.get("ok") and not trade.get("engine_up") and market_day:
        out.append("The trading bots aren't answering. Start them with: python3 ~/optionsbots/scripts/bots.py start")
    if trade.get("ok") and not trade.get("reconciled", True) and market_day:   # after the close the engine stops checking
        out.append("The trading books don't match Alpaca right now.")
    if coin.get("ok") and coin.get("age_s") is not None and coin["age_s"] > 1800:
        out.append(f"The memecoin bot has been quiet for {round(coin['age_s'] / 60)} minutes (it should check in every few minutes).")
    if batt and not batt["plugged"] and batt["pct"] < 50:
        out.append(f"The Mac is on battery at {batt['pct']}%. Plug it in so the bots keep running.")
    for c in crews:
        for a in c["agents"]:
            if a["problem"] and a["light"] != "grey":
                msg = a["last"] if len(a["last"]) <= 140 else a["last"][:137].rstrip() + "…"
                out.append(f"{c['crew']}, {a['name']}: {msg}")
    return out[:8]


def crew_line(crew: dict) -> dict:
    """One line per crew: its best light, the most recent thing any of its agents did, and whether any reported a problem."""
    seen = [a for a in crew["agents"] if a["age_s"] is not None]
    latest = min(seen, key=lambda a: a["age_s"]) if seen else None
    rank = {"green": 0, "blue": 1, "grey": 2}
    return {"light": min((a["light"] for a in crew["agents"]), key=rank.get, default="grey"),
            "latest": f"{latest['name']}: {latest['last']}" if latest else "nothing in the recent log",
            "age_s": latest["age_s"] if latest else None, "problem": any(a["problem"] and a["light"] != "grey" for a in crew["agents"])}


def snapshot() -> dict:
    now = datetime.now()
    crews = agents_status(read_log(), now)
    counts = {c["crew"]: c["items"] for c in numbers(now)}
    for c in crews:
        c["numbers"] = counts.get(c["crew"], [])
        c.update(crew_line(c))
    trade, coin, batt = trading(), memecoin(), _battery()
    agents = [a for c in crews for a in c["agents"]]
    return {"at": now.strftime("%-I:%M %p"), "trading": trade, "memecoin": coin, "memecoin_feed": memecoin_feed(),
            "shop": shop_money(now), "battery": batt, "crews": crews, "attention": attention(crews, trade, coin, batt),
            "summary": {"agents": len(agents), "active_now": sum(a["light"] == "green" for a in agents),
                        "active_today": sum(a["light"] in ("green", "blue") for a in agents),
                        "problems": sum(a["problem"] for a in agents)}}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio</title><style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#12161d;--mute:#6b7380;--line:#e6e8ec;--up:#0c8a4c;--down:#cf3a24;--blue:#2f6fde;--grey:#a3aab5;--warn-bg:#fff6e5;--warn:#8a5a00;--soft:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--bg:#0b0e13;--card:#141922;--ink:#e9edf3;--mute:#8b94a3;--line:#232a35;--up:#38c97f;--down:#f0634e;--blue:#6aa2ff;--grey:#5b6472;--warn-bg:#2b2210;--warn:#f2c46b;--soft:#1b212c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif;padding:20px 16px 56px;-webkit-font-smoothing:antialiased}
main{max-width:980px;margin:0 auto}a{color:var(--blue);text-decoration:none}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{margin:0;font-size:22px;letter-spacing:-.01em}.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--mute);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--grey)}.pill.open i{background:var(--up);animation:pulse 1.4s infinite}.pill b{color:var(--ink);font-weight:600}@keyframes pulse{50%{opacity:.3}}
.seg{display:inline-flex;background:var(--soft);border-radius:10px;padding:3px}.seg button{border:0;background:transparent;color:var(--mute);font:inherit;font-size:14px;padding:6px 14px;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--card);color:var(--ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin-bottom:14px}
h2{font-size:17px;margin:0 0 12px;letter-spacing:-.01em}.sub{color:var(--mute);font-size:13px}
.up{color:var(--up)}.down{color:var(--down)}
.hero{position:relative}.proj{position:absolute;top:16px;right:18px;text-align:right;max-width:250px;font-size:12px;color:var(--mute);line-height:1.35}.proj .pv{font-size:20px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums;margin:1px 0}.proj .pv.up{color:var(--up)}.proj .pv.down{color:var(--down)}.proj details{margin-top:3px}.proj summary{cursor:pointer;color:var(--blue);list-style:none}.proj summary::-webkit-details-marker{display:none}.proj .how{text-align:left;margin-top:6px;background:var(--soft);border-radius:10px;padding:8px 10px;color:var(--ink)}@media (max-width:640px){.proj{position:static;text-align:left;max-width:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}}.hero .acct{color:var(--mute);font-size:13px}.hero .val{font-size:40px;font-weight:700;letter-spacing:-.02em;line-height:1.15;margin:2px 0;font-variant-numeric:tabular-nums}
.hero .chg{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}.hero .chg span{color:var(--mute);font-weight:400;margin-left:4px}
#eqchart{height:260px;margin:14px -6px 6px}.ranges{display:flex;gap:4px;border-top:1px solid var(--line);padding-top:10px}
.ranges button{border:0;background:transparent;color:var(--mute);font:inherit;font-size:13px;font-weight:600;padding:5px 12px;border-radius:8px;cursor:pointer}.ranges button.on{background:var(--soft);color:var(--ink)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}@media (max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px}.stat .k{color:var(--mute);font-size:12px}.stat .v{font-size:19px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}.stat .s{color:var(--mute);font-size:12px}
.banner{display:block;background:var(--warn-bg);color:var(--warn);border-radius:12px;padding:10px 14px;margin-bottom:14px;font-size:14px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}th{text-align:left;color:var(--mute);font-weight:500;font-size:12px;padding:0 0 8px;border-bottom:1px solid var(--line)}
td{padding:10px 0;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}th.r,td.r{text-align:right}td .s{color:var(--mute);font-size:12px;margin-top:1px}th+th,td+td{padding-left:12px}
@media (max-width:560px){.hide-sm{display:none}}
.empty{color:var(--mute);font-size:14px;padding:6px 0}
.acctrow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.acctrow:last-child{border-bottom:0;padding-bottom:0}.acctrow:first-of-type{padding-top:0}
.acctrow .n{font-weight:600}.acctrow .r{text-align:right;font-variant-numeric:tabular-nums}.acctrow .r b{font-size:16px}.acctrow .r .sub span{white-space:nowrap}
.chead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}.chead h2{margin:0}
.tabs{display:flex;gap:6px;flex-wrap:wrap}.tab{border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:999px;padding:4px 11px;font:inherit;font-size:13px;cursor:pointer}
.tab.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}.tab sup{font-size:10px;margin-left:3px;color:var(--up);font-weight:700}.tab.on sup{color:var(--bg)}
.daynav{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--mute)}.daynav button{border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:8px;width:28px;height:28px;cursor:pointer}
.daynav button:disabled{opacity:.35;cursor:default}.live{color:var(--up);font-weight:700;font-size:12px}
#chart{height:340px}@media (max-width:720px){#chart{height:280px}}.moves{margin-top:8px;font-size:13px}.mv{display:flex;gap:10px;padding:6px 0;border-top:1px solid var(--line)}.mv .t{color:var(--mute);min-width:84px}.mv b.buy{color:var(--up)}.mv b.sell{color:var(--down)}
.more{border:0;background:transparent;color:var(--blue);font:inherit;font-size:14px;cursor:pointer;padding:10px 0 0}
.foot{color:var(--mute);font-size:12px;text-align:center;margin-top:18px}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:12px;padding:12px 16px}.warn ul{margin:0;padding-left:18px}.warn li{margin:3px 0}.ok{color:var(--up);font-weight:600}
.crew{border-top:1px solid var(--line)}.crew:first-child{border-top:0}.crew summary{list-style:none;cursor:pointer;display:flex;gap:12px;align-items:flex-start;padding:11px 0}.crew summary::-webkit-details-marker{display:none}
.dot{width:10px;height:10px;border-radius:50%;margin-top:6px;flex:none;background:var(--grey)}.dot.green{background:var(--up)}.dot.blue{background:var(--blue)}.dot.prob{box-shadow:0 0 0 3px var(--warn)}
.cname{font-weight:600}.clast{color:var(--mute);font-size:13px;overflow-wrap:anywhere}.when{color:var(--mute);font-size:12px;margin-left:6px;font-weight:400}
.cmore{padding:0 0 12px 22px;font-size:13px;overflow-wrap:anywhere}.cmore .a{padding:5px 0}.cmore .nums{color:var(--mute);margin-bottom:6px}.cmore .nums b{color:var(--ink)}.cmore .bad{color:var(--down)}.cmore .s{color:var(--mute)}
[hidden]{display:none!important}
</style></head><body><main>
<div class="top"><div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap"><h1 id="title">Portfolio</h1><span class="pill" id="mkt"><i></i><b>…</b></span></div>
<div class="seg"><button id="t-pf" class="on">Portfolio</button><button id="t-ops">Operations</button></div></div>

<section id="pf">
 <a class="banner" id="pfwarn" hidden></a>
 <div class="card hero"><div class="acct">Options account · paper money</div><div class="val" id="val">–</div><div class="chg" id="chg"></div><div class="proj" id="proj"></div>
  <div id="eqchart"></div><div class="ranges" id="ranges"><button data-r="1D">1D</button><button data-r="1W">1W</button><button data-r="1M">1M</button><button data-r="ALL" class="on">ALL</button></div></div>
 <div class="stats" id="stats"></div>
 <div class="card"><h2>Accounts</h2><div id="accts"></div></div>
 <div class="card"><h2>Open positions</h2><div id="pos"></div></div>
 <div class="card"><div class="chead"><h2>Trades on the chart</h2><div class="daynav"><span id="live"></span><button id="prev" aria-label="Previous day">‹</button><span id="dayl"></span><button id="next" aria-label="Next day">›</button></div></div>
  <div class="tabs" id="symtabs" style="margin-bottom:10px"></div><div id="chart"></div><div class="sub" id="chartnote" style="margin-top:8px"></div><div class="moves" id="moves"></div></div>
 <div class="card"><h2>History</h2><div id="hist"></div></div>
 <div class="card"><h2>Strategies</h2><div id="strat"></div></div>
 <div class="foot">Paper trading on Alpaca: simulated money, real market prices. Detailed engine view: <a href="http://localhost:8444" target="_blank">localhost:8444</a></div>
</section>

<section id="ops" hidden>
 <div class="card"><h2>Needs a look</h2><div id="attn"></div></div>
 <div class="card"><h2>Shop</h2><div id="shop"></div></div>
 <div class="card"><h2>Crews <span class="sub" id="crewsum" style="font-weight:400"></span></h2><div id="crews"></div></div>
</section>
</main>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script>
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd=(n,sign)=>{const s=Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});return (n<0?'−':sign&&n>0?'+':'')+'$'+s};
const usd0=(n,sign)=>(n<0?'−':sign&&n>0?'+':'')+'$'+Math.abs(Math.round(n)).toLocaleString('en-US');
const pct=n=>(n>0?'+':n<0?'−':'')+Math.abs(n).toFixed(Math.abs(n)<10?2:1)+'%';
const pct1=n=>(n>0?'+':n<0?'−':'')+Math.abs(n).toFixed(1)+'%';
const cls=n=>n>0?'up':n<0?'down':'';
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const shift=t=>t-new Date(t*1000).getTimezoneOffset()*60;          // charts show the Mac's own clock
const ago=s=>s==null?'':s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';
const $id=id=>document.getElementById(id);

// ---------- tabs ----------
function show(tab){const ops=tab==='ops';$id('pf').hidden=ops;$id('ops').hidden=!ops;$id('t-pf').classList.toggle('on',!ops);$id('t-ops').classList.toggle('on',ops);
 $id('title').textContent=ops?'Operations':'Portfolio';document.title=ops?'Operations':'Portfolio';try{history.replaceState(null,'',ops?'#ops':location.pathname+location.search)}catch(e){}
 if(!ops&&EQ)setTimeout(()=>EQ.timeScale().fitContent(),0)}
$id('t-pf').onclick=()=>show('pf');$id('t-ops').onclick=()=>show('ops');$id('pfwarn').onclick=()=>show('ops');

// ---------- portfolio ----------
let PF=null,EQ=null,AREA=null,RANGE='ALL',HIST_ALL=false;
function rangePoints(){const s=PF.series;if(!s.length)return {pts:[],base:0};const lastT=s[s.length-1].t;
 if(RANGE==='1D'){const d=new Date(lastT*1000).toLocaleDateString('en-US',{timeZone:'America/New_York'});
  const day=s.filter(p=>new Date(p.t*1000).toLocaleDateString('en-US',{timeZone:'America/New_York'})===d);const i=s.indexOf(day[0]);return {pts:day,base:i>0?s[i-1].v:day[0].v}}
 const span={'1W':7,'1M':30}[RANGE];const pts=span?s.filter(p=>p.t>=lastT-span*86400):s;const i=s.indexOf(pts[0]);return {pts,base:i>0?s[i-1].v:pts[0].v}}
const RLABEL={'1D':'Today','1W':'Past week','1M':'Past month','ALL':'All time'};
function setHead(v,base,label){$id('val').textContent=usd(v);const d=v-base,p=base?d/base*100:0;$id('chg').innerHTML=`<b class="${cls(d)}">${usd(d,true)} (${pct(p)})</b><span>${esc(label)}</span>`}
function drawEquity(){if(!PF)return;const {pts,base}=rangePoints();
 if(!EQ&&window.LightweightCharts){EQ=LightweightCharts.createChart($id('eqchart'),{autoSize:true,layout:{background:{color:'transparent'},textColor:css('--mute'),fontFamily:'-apple-system,system-ui,sans-serif'},
  grid:{vertLines:{visible:false},horzLines:{visible:false}},rightPriceScale:{visible:false},leftPriceScale:{visible:false},timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false},
  crosshair:{mode:1,horzLine:{visible:false,labelVisible:false},vertLine:{labelVisible:true,style:3}},handleScroll:false,handleScale:false});
  AREA=EQ.addAreaSeries({lineWidth:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerRadius:4});
  EQ.subscribeCrosshairMove(p=>{const r=rangePoints();if(!p.time||!p.seriesData||!p.seriesData.get(AREA)){setHead(PF.options.equity,r.base,RLABEL[RANGE]);return}
   setHead(p.seriesData.get(AREA).value,r.base,new Date(p.time*1000).toLocaleString('en-US',{timeZone:'UTC',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}))})}
 const last=pts.length?pts[pts.length-1].v:PF.options.equity;const c=last>=base?css('--up'):css('--down');
 if(AREA){AREA.applyOptions({lineColor:c,topColor:c+'33',bottomColor:c+'00'});AREA.setData(pts.map(p=>({time:shift(p.t),value:p.v})));EQ.timeScale().fitContent()}
 setHead(PF.options.equity,base,RLABEL[RANGE]);
 document.querySelectorAll('#ranges button').forEach(b=>b.classList.toggle('on',b.dataset.r===RANGE))}
$id('ranges').onclick=e=>{const b=e.target.closest('[data-r]');if(b){RANGE=b.dataset.r;drawEquity()}};

function renderProj(j){if(!j){$id('proj').innerHTML='';return}const n=j.next,r=j.review;
 const open=$id('projhow')&&$id('projhow').open;
 $id('proj').innerHTML=`<div>Projected · next ${j.horizon_days} trading days</div><div class="pv ${cls(n.median)}">${usd0(n.median,true)}</div>`+
  `<div>likely ${usd0(n.low,true)} to ${usd0(n.high,true)} · ${n.chance_up}% chance of profit</div>`+
  (r?`<div>By Jan 15 review (${j.review_days} days): <b class="${cls(r.median)}">${usd0(r.median,true)}</b></div>`:'')+
  `<details id="projhow"${open?' open':''}><summary>How is this worked out?</summary><div class="how">Based on ${j.trades} trades since Nov 16: ${j.win_rate??'–'}% won, average win ${j.avg_win!=null?usd0(j.avg_win,true):'–'}, average loss ${j.avg_loss!=null?usd0(j.avg_loss,true):'–'}, about ${j.trades_per_day} trades a day, ${j.grows?`each sized at up to ${j.trade_pct}% of the account, so trades grow (or shrink) with it`:'each within the fixed $ limits'}. `+
  `Live results are blended with the 2-year backtest (live counts ${j.live_weight}% so far; past 30 trades it leads), giving about ${j.per_trade_pct>0?'+':''}${j.per_trade_pct}% per trade. `+
  `Then 4,000 simulated stretches of trading: same-day trades win or lose together (the bots bet on related funds at the same time), the true edge itself may be off by about ${j.edge_doubt}% per trade, and the daily loss stop applies. The range covers the middle 80%. It updates after every trade. A projection, not a promise.</div></details>`}
function renderPF(){const p=PF,o=p.options,c=p.crypto;renderProj(p.projection);
 const m=p.market;$id('mkt').className='pill'+(m.open?' open':'');$id('mkt').innerHTML=`<i></i><b>${esc(m.label)}</b> ${esc(m.detail)}`;
 drawEquity();
 $id('stats').innerHTML=[['Total, both accounts',usd(p.total),''],['All-time return',`<span class="${cls(o.all_time)}">${usd0(o.all_time,true)}</span>`,`${pct(o.all_time_pct)} on ${usd0(o.start)}`],
  ['Win rate',o.win_rate==null?'–':o.win_rate+'%',`${o.trades} closed trades`],['Open positions',String(p.positions.length),p.positions.length?'right now':(m.open?'none right now':'bots hold nothing overnight')]]
  .map(s=>`<div class="stat"><div class="k">${s[0]}</div><div class="v">${s[1]}</div><div class="s">${s[2]}</div></div>`).join('');
 $id('accts').innerHTML=`<div class="acctrow"><div><div class="n">Options bots</div><div class="sub">10 bots · SPY, QQQ, IWM, TSLA, MSFT options</div></div><div class="r"><b>${usd(o.equity)}</b><div class="sub"><span class="${cls(o.today)}">${usd(o.today,true)}</span> today · <span class="${cls(o.all_time)}">${usd0(o.all_time,true)}</span> all time</div></div></div>`+
  (c.ok?`<div class="acctrow"><div><div class="n">Crypto bot</div><div class="sub">${c.holding.length?'Holding '+esc(c.holding.join(', ')):'In cash'}${c.note?' · '+esc(c.note):''}</div></div><div class="r"><b>${usd(c.equity)}</b><div class="sub"><span class="${cls(c.today)}">${usd(c.today,true)}</span> today · <span class="${cls(c.all_time)}">${usd0(c.all_time,true)}</span> all time</div></div></div>`:'');
 $id('pos').innerHTML=p.positions.length?`<table><tr><th>Position</th><th class="r hide-sm">Qty</th><th class="r hide-sm">Avg cost</th><th class="r hide-sm">Price</th><th class="r">Value</th><th class="r">Gain/loss</th></tr>`+
  p.positions.map(x=>`<tr><td>${esc(x.name)}<div class="s">${esc(x.bot)}</div></td><td class="r hide-sm">${x.qty??''}</td><td class="r hide-sm">${x.avg?usd(x.avg):''}</td><td class="r hide-sm">${x.price!=null?usd(x.price):''}</td><td class="r">${usd(x.value)}</td><td class="r ${cls(x.pnl)}">${usd(x.pnl,true)}<div class="s">${pct1(x.pct)}</div></td></tr>`).join('')+'</table>'
  :`<div class="empty">No open positions. ${m.open?'The bots buy when a setup appears.':'The bots open their first trades at '+esc(m.first_entry)+' on market days.'}</div>`;
 const rows=HIST_ALL?p.history:p.history.slice(0,12);
 $id('hist').innerHTML=p.history.length?`<table><tr><th>Closed</th><th>Position</th><th class="r hide-sm">Qty</th><th class="r hide-sm">Bought</th><th class="r hide-sm">Sold</th><th class="r">Gain/loss</th></tr>`+
  rows.map(h=>`<tr><td>${esc(h.date)}<div class="s">${esc(h.time)}</div></td><td>${esc(h.name)}<div class="s">${esc(h.bot)}</div></td><td class="r hide-sm">${h.qty??''}</td><td class="r hide-sm">${h.entry!=null?fmtPrice(h.entry):''}</td><td class="r hide-sm">${h.exit!=null?fmtPrice(h.exit):''}</td><td class="r ${cls(h.pnl)}">${usd0(h.pnl,true)}<div class="s">${pct1(h.pct)}</div></td></tr>`).join('')+'</table>'+
  (p.history.length>12?`<button class="more" id="histmore">${HIST_ALL?'Show less':'Show all '+p.history.length+' trades'}</button>`:''):'<div class="empty">No closed trades yet.</div>';
 const hm=$id('histmore');if(hm)hm.onclick=()=>{HIST_ALL=!HIST_ALL;renderPF()};
 $id('strat').innerHTML=`<table><tr><th>Strategy</th><th class="r">Trades</th><th class="r">Win rate</th><th class="r">Gain/loss</th></tr>`+
  p.strategies.map(s=>`<tr><td>${esc(s.name)}</td><td class="r">${s.trades}</td><td class="r">${s.win_rate==null?'–':s.win_rate+'%'}</td><td class="r ${cls(s.pnl)}">${s.trades?usd0(s.pnl,true):'–'}</td></tr>`).join('')+'</table>'}
const fmtPrice=v=>v<0.01?'$'+Number(v).toPrecision(3):usd(v);
async function loadPF(){try{PF=await (await fetch('/api/portfolio')).json();renderPF();CS.live=PF.market.open}catch(e){$id('val').textContent='–';$id('chg').textContent='Waiting for the worker…'}}

// ---------- candle chart with the bots' buys and sells ----------
const U0=new URLSearchParams(location.search);let CH=null,SER=null,CS={symbol:U0.get('symbol'),day:U0.get('day'),days:[],fit:true,live:false};
function makeChart(){if(!window.LightweightCharts){$id('chartnote').textContent='The chart library did not load (no internet?).';return false}
 CH=LightweightCharts.createChart($id('chart'),{autoSize:true,layout:{background:{color:'transparent'},textColor:css('--mute'),fontFamily:'-apple-system,system-ui,sans-serif'},
  grid:{vertLines:{color:css('--line')},horzLines:{color:css('--line')}},rightPriceScale:{borderColor:css('--line')},timeScale:{borderColor:css('--line'),timeVisible:true,secondsVisible:false},crosshair:{mode:0}});
 SER=CH.addCandlestickSeries({upColor:css('--up'),downColor:css('--down'),borderVisible:false,wickUpColor:css('--up'),wickDownColor:css('--down')});return true}
function moveText(m){return (m.side==='buy'?'bought ':'sold ')+`${m.qty}× ${m.symbol} $${m.strike?Number(m.strike):''} ${m.kind} @ ${usd(m.price)}`+(m.pnl!=null?` · ${usd0(m.pnl,true)} (${pct1(m.pct)})`:'')}
async function loadChart(){try{const q=new URLSearchParams();if(CS.symbol)q.set('symbol',CS.symbol);if(CS.day)q.set('day',CS.day);
 const d=await (await fetch('/api/chart?'+q)).json();const changed=d.symbol!==CS.symbol||d.day!==CS.day;Object.assign(CS,{symbol:d.symbol,day:d.day,days:d.days,live:d.live});
 if(!SER&&!makeChart())return;
 SER.setData(d.bars.map(b=>({...b,time:shift(b.time)})));
 SER.setMarkers(d.marks.map(m=>({time:shift(m.bar),position:m.side==='buy'?'belowBar':'aboveBar',color:m.side==='buy'?css('--up'):css('--down'),shape:m.side==='buy'?'arrowUp':'arrowDown',
  text:m.side==='buy'?`${m.qty}${m.kind[0].toUpperCase()}`:(m.pnl!=null?usd0(m.pnl,true):'sold')})));
 if(changed||CS.fit){CH.timeScale().fitContent();CS.fit=false}
 $id('symtabs').innerHTML=d.symbols.map(s=>`<button class="tab${s===d.symbol?' on':''}" data-s="${s}">${s}${d.counts[s]?`<sup>${d.counts[s]}</sup>`:''}</button>`).join('');
 $id('dayl').textContent=d.day_label;$id('live').innerHTML=d.live?'<span class="live">● LIVE</span>':'';
 const i=d.days.indexOf(d.day);$id('prev').disabled=i<=0;$id('next').disabled=i<0||i>=d.days.length-1;
 const n=d.marks.filter(m=>m.side==='buy').length;
 $id('chartnote').textContent=d.error?d.error:!d.bars.length?'No candles for this day (market closed).':`${d.symbol}, 1-minute candles. `+(n?`Green ▲ = a bot bought, red ▼ = it sold (${n} trade${n>1?'s':''} this day).`:`No bot trades on ${d.symbol} this day.`);
 $id('moves').innerHTML=d.marks.map(m=>`<div class="mv"><span class="t">${esc(m.at)}</span><span><b class="${m.side}">${m.side==='buy'?'BUY':'SELL'}</b> ${esc(m.name)}: ${esc(moveText(m))}</span></div>`).join('');
}catch(e){$id('chartnote').textContent='Chart is waiting for the worker…'}}
$id('symtabs').onclick=e=>{const b=e.target.closest('[data-s]');if(b){CS.symbol=b.dataset.s;CS.fit=true;loadChart()}};
$id('prev').onclick=()=>{const i=CS.days.indexOf(CS.day);if(i>0){CS.day=CS.days[i-1];CS.fit=true;loadChart()}};
$id('next').onclick=()=>{const i=CS.days.indexOf(CS.day);if(i>=0&&i<CS.days.length-1){CS.day=CS.days[i+1];CS.fit=true;loadChart()}};

// ---------- operations ----------
const openCrews=new Set();document.addEventListener('toggle',e=>{const d=e.target;if(d.dataset&&d.dataset.k){d.open?openCrews.add(d.dataset.k):openCrews.delete(d.dataset.k)}},true);
function renderOps(d){
 const n=d.attention.length;$id('pfwarn').hidden=!n;$id('pfwarn').textContent=n?`⚠ ${n} thing${n>1?'s':''} need${n>1?'':'s'} a look: ${d.attention[0].slice(0,90)}${n>1?' …':''} (see Operations)`:'';
 $id('attn').innerHTML=n?`<div class="warn"><ul>${d.attention.map(a=>`<li>${esc(a)}</li>`).join('')}</ul></div>`:'<div class="ok">All good. Nothing needs you right now.</div>';
 const s=d.shop;$id('shop').innerHTML=`<div class="acctrow"><div><div class="n">${s.sales} sale${s.sales===1?'':'s'}</div><div class="sub">${s.week} in the last 7 days</div></div><div class="r"><b>$${s.total.toFixed(2)}</b><div class="sub">total sales</div></div></div>`;
 const S=d.summary;$id('crewsum').textContent=`· ${S.active_today} of ${S.agents} agents active today`;
 const c=d.memecoin;const coinCrew={crew:'Crypto crew',light:c.ok&&c.age_s!=null&&c.age_s<1800?'green':'grey',latest:d.memecoin_feed[0]?d.memecoin_feed[0].from+': '+d.memecoin_feed[0].text:'quiet',age_s:c.age_s,problem:false,numbers:[],agents:[],feed:d.memecoin_feed};
 $id('crews').innerHTML=[...d.crews,coinCrew].map(k=>`<details class="crew" data-k="${esc(k.crew)}"${openCrews.has(k.crew)?' open':''}><summary><span class="dot ${k.light}${k.problem?' prob':''}"></span><div><div class="cname">${esc(k.crew)}<span class="when">${ago(k.age_s)}</span></div><div class="clast">${esc(k.latest)}</div></div></summary><div class="cmore">`+
  (k.numbers.length?`<div class="nums">${k.numbers.map(x=>`<b>${esc(x[1])}</b> ${esc(x[0])}`).join(' · ')}</div>`:'')+
  k.agents.map(a=>`<div class="a"><span class="dot ${a.light}" style="display:inline-block;margin:0 6px 0 0"></span><b>${esc(a.name)}</b><span class="when">${ago(a.age_s)}</span><div class="${a.problem?'bad':'s'}">${esc(a.last)}</div></div>`).join('')+
  (k.feed?k.feed.map(m=>`<div class="a"><b>${esc(m.from)} → ${esc(m.to)}</b><div>${esc(m.text)}</div></div>`).join('')||'<div>Nothing yet.</div>':'')+`</div></details>`).join('')}
async function loadOps(){try{renderOps(await (await fetch('/api/agents')).json())}catch(e){}}

if(location.hash==='#ops')show('ops');
loadPF();loadChart();loadOps();
setInterval(loadPF,15000);setInterval(loadOps,15000);setInterval(()=>{if(CS.live)loadChart()},5000);
</script></body></html>
"""




class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/api/agents":
            body, kind = json.dumps(snapshot()).encode(), "application/json"
        elif self.path.split("?")[0] == "/api/portfolio":
            from worker import home_portfolio
            body, kind = json.dumps(home_portfolio.portfolio()).encode(), "application/json"
        elif self.path.split("?")[0] == "/api/chart":
            from urllib.parse import parse_qs, urlsplit
            from worker import home_chart
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            body, kind = json.dumps(home_chart.chart(q.get("symbol"), q.get("day"))).encode(), "application/json"
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start(log=print, port: int = PORT) -> Optional[ThreadingHTTPServer]:
    """Serve the page on localhost in a background thread. Never raises: a busy port just means no page."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log(f"agents HQ: couldn't open port {port}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="agents-hq").start()
    log(f"agents HQ: http://localhost:{port}")
    return server


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        print(json.dumps(snapshot(), indent=1)[:4000])
    else:
        start()
        print(f"http://localhost:{PORT}  (Ctrl-C to stop)")
        while True:
            time.sleep(3600)
