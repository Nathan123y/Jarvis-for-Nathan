"""Home: the one dashboard. Money first (the paper trading account, the memecoin bot, shop sales), then what needs a look,
the trading bots' scoreboard, and one line per crew (click a crew for its agents). Replaces the separate Agents HQ page;
the detailed trading dashboard stays at http://localhost:8444 and is linked from here.

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
PROBLEM = re.compile(r"fail|couldn't|could not|error|refused|flaw|timed out|wrong", re.I)

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
        ("Scout", "looks for new openings", r"scout|found|new"),
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


def round_trips(rows) -> list:
    """Closed trades from fills [(bot, side, contract_id, contract_json, qty, price, ts)] in order. A trade runs from the first
    buy of a contract until the bot holds none of it again, so an order Alpaca fills in pieces is still one trade."""
    held, out = {}, []
    for bot, side, cid, cj, qty, price, ts in rows:
        qty, price, k = int(qty), float(price), (bot, cid)
        t = held.get(k)
        if side.startswith("BUY"):
            if t is None:
                try:
                    c = json.loads(cj or "{}")
                except ValueError:
                    c = {}
                t = held[k] = {"bot": bot, "symbol": c.get("underlying", cid[:4]), "kind": "put" if c.get("right") == "P" else "call",
                               "qty": 0, "open": 0, "cost": 0.0, "got": 0.0, "opened": _ny(ts)}
            t["qty"] += qty
            t["open"] += qty
            t["cost"] += qty * price
        elif t is not None:
            n = min(qty, t["open"])
            t["got"] += n * price
            t["open"] -= n
            if t["open"] <= 0:
                t["closed"] = _ny(ts)
                t["pnl"] = round((t["got"] - t["cost"]) * 100, 2)
                t["pct"] = round((t["got"] / t["cost"] - 1) * 100, 1) if t["cost"] else 0.0
                del t["open"], t["got"]
                out.append(t)
                del held[k]
    return out


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
    market_day = now_ny.weekday() < 5 and 6 <= now_ny.hour < 17
    if trade.get("ok") and not trade.get("engine_up") and market_day:
        out.append("The trading bots aren't answering. Start them with: python3 ~/optionsbots/scripts/bots.py start")
    if trade.get("ok") and not trade.get("reconciled", True):
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
<title>Home</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--mute:#6b7380;--line:#e4e7ec;--up:#0f8a4f;--down:#c2412d;--blue:#2f6fde;--grey:#a3aab5;--warn-bg:#fff6e5;--warn:#8a5a00;--bar:#d9dde3}
@media (prefers-color-scheme:dark){:root{--bg:#0c0f14;--card:#151a22;--ink:#e8ecf2;--mute:#8b94a3;--line:#252c38;--up:#3ccf86;--down:#f06a55;--blue:#6aa2ff;--grey:#5b6472;--warn-bg:#2b2210;--warn:#f2c46b;--bar:#2a313d}}
*{box-sizing:border-box}.money>*,.two>*{min-width:0}.clast,.warn,.more{overflow-wrap:anywhere}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif;padding:24px 16px 48px}
main{max-width:1040px;margin:0 auto}header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:18px}
h1{margin:0;font-size:24px;letter-spacing:-.01em}#at{color:var(--mute);font-size:13px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:28px 0 10px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.money{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px}@media (max-width:760px){.money{grid-template-columns:1fr}}
.label{color:var(--mute);font-size:13px}.big{font-size:40px;font-weight:700;letter-spacing:-.02em;line-height:1.1;margin:4px 0}.mid{font-size:26px;font-weight:700;margin:4px 0}
.up{color:var(--up)}.down{color:var(--down)}.row{display:flex;gap:18px;flex-wrap:wrap;font-size:14px}.row b{font-weight:600}
.week{display:flex;gap:8px;align-items:flex-end;height:96px;margin-top:14px}.day{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:12px;color:var(--mute)}
.day i{display:block;width:100%;max-width:46px;border-radius:5px 5px 0 0;background:var(--bar);min-height:3px}.day i.up{background:var(--up)}.day i.down{background:var(--down)}.day em{font-style:normal;font-weight:600;color:var(--ink);font-size:12px}
.note{color:var(--mute);font-size:13px;margin-top:8px}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:12px;padding:12px 16px}.warn li{margin:3px 0}.warn ul{margin:0;padding-left:18px}.ok{color:var(--up);font-weight:600}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media (max-width:760px){.two{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:7px 0;border-top:1px solid var(--line)}tr:first-child td{border-top:0}td.n{text-align:right;white-space:nowrap;padding-left:10px}td .s{color:var(--mute);font-size:12px}
.crew{border-top:1px solid var(--line)}.crew:first-child{border-top:0}.crew summary{list-style:none;cursor:pointer;display:flex;gap:12px;align-items:flex-start;padding:11px 0}.crew summary::-webkit-details-marker{display:none}
.dot{width:10px;height:10px;border-radius:50%;margin-top:6px;flex:none;background:var(--grey)}.dot.green{background:var(--up)}.dot.blue{background:var(--blue)}.dot.prob{box-shadow:0 0 0 3px var(--warn)}
.cname{font-weight:600}.clast{color:var(--mute);font-size:13px;word-break:break-word}.when{color:var(--mute);font-size:12px;margin-left:6px;font-weight:400}
.more{padding:0 0 12px 22px;font-size:13px}.more .a{padding:5px 0}.more .nums{color:var(--mute);margin-bottom:6px}.more .nums b{color:var(--ink)}.more .bad{color:var(--down)}
a{color:var(--blue);text-decoration:none}
</style></head><body><main>
<header><h1>Home</h1><span id="at">loading…</span></header>
<div class="money">
 <div class="card"><div class="label">Trading account (paper)</div><div class="big" id="eq">–</div><div class="row" id="eqrow"></div><div class="week" id="week"></div><div class="note" id="drive"></div></div>
 <div class="card"><div class="label">Memecoin bot (paper)</div><div class="mid" id="coin">–</div><div class="row" id="coinrow"></div><div class="note" id="coinnote"></div></div>
 <div class="card"><div class="label">Shop sales</div><div class="mid" id="shop">–</div><div class="row" id="shoprow"></div></div>
</div>
<h2>Needs a look</h2><div id="attn"></div>
<div class="two">
 <div><h2>Trading bots</h2><div class="card"><table id="bots"></table><div class="note"><a href="http://localhost:8444" target="_blank">Full trading detail →</a></div></div></div>
 <div><h2>Latest trades</h2><div class="card"><table id="recent"></table></div></div>
</div>
<h2>Crews <span id="crewsum" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h2><div class="card" id="crews"></div>
</main><script>
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $=(n,s=true)=>{const v=Math.abs(n).toLocaleString('en-US',{maximumFractionDigits:0});return (n<0?'−':s&&n>0?'+':'')+'$'+v};
const cls=n=>n>0?'up':n<0?'down':'';
const ago=s=>s==null?'':s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';
const open=new Set();
document.addEventListener('toggle',e=>{const d=e.target;if(d.dataset&&d.dataset.k){d.open?open.add(d.dataset.k):open.delete(d.dataset.k)}},true);
function render(d){
 document.getElementById('at').textContent='Updated '+d.at;
 const t=d.trading;
 if(t.ok){document.getElementById('eq').textContent=$(t.equity,false);
  document.getElementById('eqrow').innerHTML=`<span>Today <b class="${cls(t.today)}">${$(t.today)}</b></span><span>This week <b class="${cls(t.week)}">${$(t.week)}</b></span><span>Since start <b class="${cls(t.since_start)}">${$(t.since_start)}</b></span>`+(t.open_positions?`<span>${t.open_positions} open now</span>`:'');
  const mx=Math.max(1,...t.days.map(x=>Math.abs(x.pnl)));
  document.getElementById('week').innerHTML=t.days.map(x=>`<div class="day" title="${x.trades} trades"><em class="${cls(x.pnl)}">${x.future?'':x.trades?$(x.pnl):'–'}</em><i class="${cls(x.pnl)}" style="height:${x.future?3:Math.max(3,Math.abs(x.pnl)/mx*62)}px"></i>${x.day}</div>`).join('');
  const dr=t.drive;document.getElementById('drive').textContent=dr.trades?`Open-drive bots: ${dr.trades} trades, ${dr.wins} won, ${dr.avg_pct>0?'+':''}${dr.avg_pct}% average (backtest about +4%; judge after 30 trades).`:'';
  document.getElementById('bots').innerHTML=t.bots.map(b=>`<tr><td>${esc(b.name)}<div class="s">${b.trades?b.trades+(b.trades===1?' trade, ':' trades, ')+b.wins+' won':'no trades yet'}</div></td><td class="n ${cls(b.pnl)}">${b.trades?$(b.pnl):''}</td></tr>`).join('');
  document.getElementById('recent').innerHTML=t.recent.length?t.recent.map(r=>`<tr><td>${esc(r.what)}<div class="s">${esc(r.name)} · ${esc(r.when)}</div></td><td class="n ${cls(r.pnl)}">${$(r.pnl)}<div class="s">${r.pct>0?'+':''}${r.pct}%</div></td></tr>`).join(''):'<tr><td class="s">No trades yet.</td></tr>';
 }else{document.getElementById('eq').textContent='Not available';}
 const c=d.memecoin;
 if(c.ok){document.getElementById('coin').textContent=$(c.equity,false);
  document.getElementById('coinrow').innerHTML=`<span>Today <b class="${cls(c.today)}">${$(c.today)}</b></span><span>Since start <b class="${cls(c.since_start)}">${$(c.since_start)}</b></span>`;
  document.getElementById('coinnote').textContent=(c.holding.length?'Holding '+c.holding.join(', ')+'. ':'In cash. ')+(c.note||'');}
 const s=d.shop;document.getElementById('shop').textContent=s.sales+(s.sales===1?' sale':' sales');
 document.getElementById('shoprow').innerHTML=`<span>Total <b>$${s.total.toFixed(2)}</b></span><span>Last 7 days <b>${s.week}</b></span>`;
 document.getElementById('attn').innerHTML=d.attention.length?`<div class="warn"><ul>${d.attention.map(a=>`<li>${esc(a)}</li>`).join('')}</ul></div>`:'<div class="card ok">All good. Nothing needs you right now.</div>';
 const S=d.summary;document.getElementById('crewsum').textContent=`· ${S.active_today} of ${S.agents} agents active today. Click a crew for details.`;
 const coinCrew={crew:'Memecoin crew',light:c.ok&&c.age_s!=null&&c.age_s<1800?'green':'grey',latest:d.memecoin_feed[0]?d.memecoin_feed[0].from+': '+d.memecoin_feed[0].text:'quiet',age_s:c.age_s,problem:false,numbers:[],agents:[],feed:d.memecoin_feed};
 document.getElementById('crews').innerHTML=[...d.crews,coinCrew].map(k=>`<details class="crew" data-k="${esc(k.crew)}"${open.has(k.crew)?' open':''}><summary><span class="dot ${k.light}${k.problem?' prob':''}"></span><div><div class="cname">${esc(k.crew)}<span class="when">${ago(k.age_s)}</span></div><div class="clast">${esc(k.latest)}</div></div></summary><div class="more">`+
  (k.numbers.length?`<div class="nums">${k.numbers.map(n=>`<b>${esc(n[1])}</b> ${esc(n[0])}`).join(' · ')}</div>`:'')+
  k.agents.map(a=>`<div class="a"><span class="dot ${a.light}" style="display:inline-block;margin:0 6px 0 0"></span><b>${esc(a.name)}</b><span class="when">${ago(a.age_s)}</span><div class="${a.problem?'bad':'s'}">${esc(a.last)}</div></div>`).join('')+
  (k.feed?k.feed.map(m=>`<div class="a"><b>${esc(m.from)} → ${esc(m.to)}</b><div>${esc(m.text)}</div></div>`).join('')||'<div>Nothing yet.</div>':'')+`</div></details>`).join('');
}
async function tick(){try{render(await (await fetch('/api/agents')).json())}catch(e){document.getElementById('at').textContent='waiting for the worker…'}}
tick();setInterval(tick,15000);
</script></body></html>"""




class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/api/agents":
            body, kind = json.dumps(snapshot()).encode(), "application/json"
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
