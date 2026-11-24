"""The Home page's Portfolio tab: the paper trading accounts laid out like a brokerage app.

    the options account's value over time (from the engine's 10-minute account snapshots), today's and all-time change,
    both accounts (options bots and the crypto bot), open positions, the trade history and how each strategy is doing.

Everything is read-only: the options bots' database (~/optionsbots/data/native/paper.db) and the crypto bot's status and
trade log (~/optionsbots/data/memebot/).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

OB = Path.home() / "optionsbots"
DB = OB / "data" / "native" / "paper.db"
COIN_STATUS = OB / "data" / "memebot" / "status.json"
COIN_TRADES = OB / "data" / "memebot" / "trades.jsonl"
NY = ZoneInfo("America/New_York")
KINDS = {"drive": "Open drive", "orb-scalp": "Opening-range scalp", "orb-day": "Opening-range day"}


def bot_label(bot_id: str) -> str:
    """'qqq-orb-scalp' -> 'QQQ · Opening-range scalp'"""
    sym, _, kind = bot_id.partition("-")
    return f"{sym.upper()} · {KINDS.get(kind, kind)}"


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# ---- market hours (NYSE rules, same as the bots' own calendar) ----

def _nth(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last(year, month, weekday):
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(y, month, day + 1)


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset:
    hs = {_nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _easter(year) - timedelta(days=2), _last(year, 5, 0),
          _observed(date(year, 6, 19)), _observed(date(year, 7, 4)), _nth(year, 9, 0, 1), _nth(year, 11, 3, 4),
          _observed(date(year, 12, 25))}
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        hs.add(date(year, 1, 2))
    elif ny.weekday() != 5:
        hs.add(ny)
    return frozenset(hs)


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def close_time(d: date) -> time:
    early = d == _nth(d.year, 11, 3, 4) + timedelta(days=1) or (d.month == 12 and d.day == 24) or \
        (d.month == 7 and d.day == 3 and d.weekday() <= 3)
    return time(13, 0) if early else time(16, 0)


def market(now: Optional[datetime] = None) -> dict:
    """{open, label, detail} in the viewer's own clock (the Mac's local time)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    d = now.date()
    local = lambda dt: dt.astimezone().strftime("%-I:%M %p")
    if trading_day(d) and time(9, 30) <= now.time() < close_time(d):
        return {"open": True, "label": "Market open", "detail": f"bots trading · closes {local(datetime.combine(d, close_time(d), NY))}",
                "first_entry": local(datetime.combine(d, time(9, 45), NY))}
    nxt = d if trading_day(d) and now.time() < time(9, 30) else d + timedelta(days=1)
    while not trading_day(nxt):
        nxt += timedelta(days=1)
    opens = datetime.combine(nxt, time(9, 30), NY)
    here, there = now.astimezone().date(), opens.astimezone().date()       # "today"/"tomorrow" on the viewer's own clock
    day = "today" if there == here else "tomorrow" if there == here + timedelta(days=1) else opens.astimezone().strftime("%A")
    why = " (holiday)" if d.weekday() < 5 and not trading_day(d) else ""
    return {"open": False, "label": "Market closed" + why, "detail": f"opens {day} at {local(opens)}",
            "first_entry": local(datetime.combine(nxt, time(9, 45), NY))}


# ---- the options account ----

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
                               "strike": c.get("strike"), "expiration": c.get("expiration"),
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
                t["entry"], t["exit"] = round(t["cost"] / t["qty"], 2), round(t["got"] / t["qty"], 2)
                del t["open"], t["got"]
                out.append(t)
                del held[k]
    return out


def _ny(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(NY)


def option_name(symbol: str, strike, kind: str, expiration: Optional[str]) -> str:
    """'QQQ $635 Put · Nov 24'"""
    s = f"{symbol} ${float(strike):g} {kind.title()}" if strike not in (None, "") else f"{symbol} {kind.title()}"
    try:
        s += " · " + date.fromisoformat(expiration).strftime("%b %-d")
    except (TypeError, ValueError):
        pass
    return s


def _read_db(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        fills = con.execute("select bot_id, side, contract_id, contract_json, quantity, price, ts from fills order by seq").fetchall()
        snaps = con.execute("select ts, payload_json from events where type='ACCOUNT_SNAPSHOT' order by seq").fetchall()
    finally:
        con.close()
    return fills, snaps


def coin_trips(path: Path = COIN_TRADES) -> list:
    """The crypto bot's closed trades from its trade log."""
    out, open_ = [], {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        sym = str(r.get("symbol", "")).replace("/USD", "")
        if r.get("kind") == "buy":
            open_[sym] = r
        elif r.get("kind") == "sell":
            b = open_.pop(sym, {})
            spent = float(r.get("spent") or b.get("usd") or 0)
            pnl = float(r.get("pnl") or 0)
            out.append({"bot": "crypto", "symbol": sym, "name": sym, "qty": None, "entry": b.get("price"), "exit": r.get("price"),
                        "pnl": round(pnl, 2), "pct": round(pnl / spent * 100, 1) if spent else 0.0,
                        "opened": _ny(b["ts"]) if b.get("ts") else None, "closed": _ny(r["ts"]), "why": r.get("why")})
    return out


def portfolio(now: Optional[datetime] = None, db: Path = DB, coin_status: Path = COIN_STATUS, coin_trades: Path = COIN_TRADES) -> dict:
    now = (now or datetime.now(NY)).astimezone(NY)
    try:
        fills, snaps = _read_db(db)
    except sqlite3.Error:
        fills, snaps = [], []
    last = json.loads(snaps[-1][1]) if snaps else {}
    last_ts = _ny(snaps[-1][0]) if snaps else None
    live = bool(last_ts) and last_ts.date() == now.date()
    trips = round_trips(fills)
    equity = float(last.get("equity") or 0)
    start = float(last.get("starting_cash") or 0)

    # value over time: one point per snapshot (every 10 minutes while the market is open)
    series = []
    for ts, p in snaps:
        try:
            v = float(json.loads(p).get("equity") or 0)
        except ValueError:
            continue
        if v > 0:
            series.append({"t": int(_ny(ts).timestamp()), "v": round(v, 2)})

    # today's change: against the last value before today's session
    before = [s["v"] for s in series if datetime.fromtimestamp(s["t"], NY).date() < now.date()]
    prev_close = before[-1] if before else start
    today = round(equity - prev_close, 2) if live else 0.0

    # open positions (only while today's snapshot is fresh; overnight the bots hold nothing)
    positions = []
    for p in (last.get("positions") or []) if live else []:
        c = p.get("contract") or {}
        qty, avg, mark = int(p.get("quantity") or 0), float(p.get("avg_price") or 0), float(p.get("mark") or 0)
        positions.append({"account": "Options", "name": option_name(c.get("underlying", "?"), c.get("strike"), "put" if c.get("right") == "P" else "call", c.get("expiration")),
                          "qty": qty, "avg": avg, "price": mark, "value": round(mark * qty * 100, 2),
                          "pnl": round(float(p.get("unrealized") or 0), 2), "pct": round((mark / avg - 1) * 100, 1) if avg else 0.0,
                          "bot": bot_label(p.get("bot_id", ""))})

    coin = _json(coin_status) or {}
    c_eq = float(coin.get("equity") or 0)
    for p in coin.get("positions") or []:
        entry, mid, spent = float(p.get("entry") or 0), p.get("mid"), float(p.get("spent") or 0)
        value = spent * (mid / entry) if mid and entry else spent
        positions.append({"account": "Crypto", "name": str(p.get("symbol", "")).replace("/USD", ""), "qty": None, "avg": entry,
                          "price": mid, "value": round(value, 2), "pnl": round(value - spent, 2),
                          "pct": round(p.get("change_pct") or 0, 1), "bot": "Crypto bot"})

    # history: newest first, both accounts
    hist = [{"account": "Options", "name": option_name(t["symbol"], t.get("strike"), t["kind"], t.get("expiration")),
             "qty": t["qty"], "entry": t["entry"], "exit": t["exit"], "pnl": round(t["pnl"]), "pct": t["pct"],
             "bot": bot_label(t["bot"]), "opened": t["opened"], "closed": t["closed"]} for t in trips]
    hist += [{"account": "Crypto", "name": t["name"], "qty": None, "entry": t["entry"], "exit": t["exit"], "pnl": round(t["pnl"]),
              "pct": t["pct"], "bot": "Crypto bot", "opened": t["opened"], "closed": t["closed"]} for t in coin_trips(coin_trades)]
    hist.sort(key=lambda h: h["closed"], reverse=True)
    for h in hist:
        h["date"] = h["closed"].astimezone().strftime("%b %-d")
        h["time"] = (h["opened"].astimezone().strftime("%-I:%M") + "–" if h["opened"] else "") + h["closed"].astimezone().strftime("%-I:%M %p")
        h["opened"] = h["opened"].isoformat() if h["opened"] else None
        h["closed"] = h["closed"].isoformat()

    # strategies: one row per bot
    strat = {}
    for t in trips:
        s = strat.setdefault(t["bot"], {"name": bot_label(t["bot"]), "trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += t["pnl"] > 0
        s["pnl"] += t["pnl"]
    for bid in last.get("per_bot") or {}:
        strat.setdefault(bid, {"name": bot_label(bid), "trades": 0, "wins": 0, "pnl": 0.0})
    strategies = sorted(({**s, "pnl": round(s["pnl"]), "win_rate": round(s["wins"] / s["trades"] * 100) if s["trades"] else None}
                         for s in strat.values()), key=lambda s: -s["pnl"])
    wins = sum(t["pnl"] > 0 for t in trips)

    if live and series:
        series[-1] = {"t": series[-1]["t"], "v": round(equity, 2)}
    return {"market": market(now),
            "options": {"equity": round(equity, 2), "start": round(start, 2), "all_time": round(equity - start, 2),
                        "all_time_pct": round((equity / start - 1) * 100, 1) if start else 0.0, "today": today,
                        "today_pct": round(today / prev_close * 100, 2) if prev_close else 0.0,
                        "trades": len(trips), "win_rate": round(wins / len(trips) * 100) if trips else None,
                        "open": sum(1 for p in positions if p["account"] == "Options")},
            "crypto": {"ok": bool(coin), "equity": round(c_eq, 2), "start": float(coin.get("first_equity") or 0),
                       "all_time": round(c_eq - float(coin.get("first_equity") or c_eq), 2),
                       "today": round(c_eq - float(coin.get("day_start_equity") or c_eq), 2),
                       "holding": [p["name"] for p in positions if p["account"] == "Crypto"],
                       "note": (coin.get("notes") or [""])[-1][6:] if coin.get("notes") else ""},
            "total": round(equity + c_eq, 2), "series": series, "positions": positions, "history": hist[:40],
            "strategies": strategies}
