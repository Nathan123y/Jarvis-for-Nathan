"""The Home page's live trading chart: 1-minute candles of a stock the paper bots trade, with every buy and sell the bots made
on it drawn on the candle where it happened.

Candles come from Alpaca's market data (the same IEX feed the bots trade on), read with the paper account's keys from
~/optionsbots/.env.paper. The keys stay in this process; the page only ever sees candles and trades. The buys and sells come
from the bots' own trade database (~/optionsbots/data/native/paper.db, read-only). The bots trade options, so a marker sits on
the stock's candle for the minute the option was bought or sold, and its label says which option, how many and at what price.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

OB = Path.home() / "optionsbots"
ENV = OB / ".env.paper"
DB = OB / "data" / "native" / "paper.db"
NY = ZoneInfo("America/New_York")
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
SYMBOLS = ["SPY", "QQQ", "IWM", "TSLA", "MSFT"]
KINDS = {"drive": "open-drive", "orb-scalp": "opening-range scalp", "orb-day": "opening-range day"}

_cache: dict = {}
_lock = threading.Lock()


def bot_label(bot_id: str) -> str:
    sym, _, kind = bot_id.partition("-")
    return f"{sym.upper()} {KINDS.get(kind, kind)}".strip()


def _keys(path: Path = ENV) -> tuple:
    vals = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return vals.get("ALPACA_PAPER_KEY_ID"), vals.get("ALPACA_PAPER_SECRET_KEY")


def _fetch(params: dict) -> list:
    """Alpaca's raw bars for one symbol, following page tokens. [] without keys."""
    kid, secret = _keys()
    if not kid or not secret:
        return []
    out, token = [], None
    while True:
        p = dict(params, **({"page_token": token} if token else {}))
        req = urllib.request.Request(f"{BARS_URL}?{urllib.parse.urlencode(p)}",
                                     headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": secret})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        out += (data.get("bars") or {}).get(params["symbols"]) or []
        token = data.get("next_page_token")
        if not token:
            return out


def bars(symbol: str, day: date, fetch: Callable[[dict], list] = None, now: Optional[datetime] = None) -> list:
    """1-minute regular-session candles [{time: epoch seconds, open, high, low, close}] for one New York trading day.
    Past days are cached for good; today's for 10 seconds."""
    today = (now or datetime.now(NY)).astimezone(NY).date()
    key = (symbol, day.isoformat())
    with _lock:
        hit = _cache.get(key)
        if hit and (day < today or time.time() - hit[0] < 10):
            return hit[1]
    start, end = datetime.combine(day, dtime(9, 30), NY), datetime.combine(day, dtime(16, 0), NY)
    raw = (fetch or _fetch)({"symbols": symbol, "timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(),
                             "feed": "iex", "limit": 10000})
    out = [{"time": int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()),
            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]} for b in raw]
    out = [b for b in out if start.timestamp() <= b["time"] < end.timestamp()]
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def _utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")


def moves(day: date, db: Path = DB) -> list:
    """Every buy and sell the bots made on a New York day, oldest first. An order Alpaca filled in pieces is one move.
    [{symbol, bot, name, side, qty, kind, strike, price, ts (epoch s), pnl, pct}]"""
    lo, hi = datetime.combine(day, dtime(0), NY), datetime.combine(day + timedelta(days=1), dtime(0), NY)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = con.execute("select bot_id, side, contract_id, contract_json, quantity, price, ts, client_order_id from fills "
                           "where ts >= ? and ts < ? order by seq", (_utc(lo), _utc(hi))).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    merged: dict = {}
    for bot, side, cid, cj, qty, price, ts, coid in rows:
        k = coid or f"{bot}{cid}{ts}"
        m = merged.get(k)
        if m:
            m["cost"] += int(qty) * float(price)
            m["qty"] += int(qty)
            continue
        try:
            c = json.loads(cj or "{}")
        except ValueError:
            c = {}
        merged[k] = {"symbol": c.get("underlying") or cid[:3], "bot": bot, "name": bot_label(bot), "cid": cid,
                     "side": "buy" if side.startswith("BUY") else "sell", "qty": int(qty), "cost": int(qty) * float(price),
                     "kind": "put" if c.get("right") == "P" else "call", "strike": c.get("strike"),
                     "ts": datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()}
    held: dict = {}
    out = []
    for m in merged.values():
        m["price"] = round(m["cost"] / m["qty"], 2) if m["qty"] else 0.0
        k = (m["bot"], m["cid"])
        h = held.setdefault(k, [0, 0.0])
        m["pnl"] = m["pct"] = None
        if m["side"] == "buy":
            h[0] += m["qty"]
            h[1] += m["cost"]
        elif h[0]:
            avg = h[1] / h[0]
            m["pnl"] = round((m["price"] - avg) * m["qty"] * 100)
            m["pct"] = round((m["price"] / avg - 1) * 100, 1)
            n = min(m["qty"], h[0])
            h[1] -= avg * n
            h[0] -= n
        del m["cost"]
        out.append(m)
    return out


def _snap(bar_times: list, ts: float) -> Optional[int]:
    """The candle a move belongs to: the last candle that started at or before it."""
    best = None
    for t in bar_times:
        if t <= ts:
            best = t
        else:
            break
    return best if best is not None else (bar_times[0] if bar_times else None)


def chart(symbol: Optional[str] = None, day: Optional[str] = None, now: Optional[datetime] = None,
          fetch: Callable[[dict], list] = None, db: Path = DB) -> dict:
    """Everything the chart needs for one symbol and day. Without a day: today, or the last day that had candles.
    Without a symbol: the one the bots last traded that day, else SPY."""
    now = (now or datetime.now(NY)).astimezone(NY)
    today = now.date()
    if day:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            d = today
    else:
        d = today
    picked_sym = symbol.upper() if symbol and symbol.upper() in SYMBOLS else None
    error = None
    for _ in range(7):                                   # walk back over weekends and holidays to a day with candles
        if d.weekday() < 5 and (d < today or now.time() >= dtime(9, 30)):
            try:
                day_moves = moves(d, db)
                sym = picked_sym or (day_moves[-1]["symbol"] if day_moves and day_moves[-1]["symbol"] in SYMBOLS else "SPY")
                candles = bars(sym, d, fetch, now)
            except Exception as exc:                     # network or Alpaca trouble: say so instead of breaking the page
                error, candles, day_moves, sym = f"couldn't load prices ({type(exc).__name__})", [], [], picked_sym or "SPY"
            if candles or day or error:
                break
        d -= timedelta(days=1)
    else:
        day_moves, candles, sym = [], [], picked_sym or "SPY"
    times = [b["time"] for b in candles]
    marks = []
    for m in day_moves:
        if m["symbol"] != sym:
            continue
        t = _snap(times, m["ts"])
        if t is not None:
            marks.append({**m, "bar": t, "at": datetime.fromtimestamp(m["ts"]).strftime("%-I:%M:%S %p")})
    counts = {s: sum(1 for m in day_moves if m["symbol"] == s and m["side"] == "buy") for s in SYMBOLS}
    days, back = [], today
    while len(days) < 10:
        if back.weekday() < 5:
            days.append(back.isoformat())
        back -= timedelta(days=1)
    open_now = d == today and today.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)
    return {"symbol": sym, "day": d.isoformat(), "day_label": d.strftime("%a %b %-d"), "live": open_now, "bars": candles,
            "marks": marks, "counts": counts, "symbols": SYMBOLS, "days": days[::-1], "error": error,
            "last": candles[-1]["close"] if candles else None}
