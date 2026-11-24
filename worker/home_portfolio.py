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



# ---- profit projection (the small box in the Portfolio's corner) ----

LINEUP_START = date(2026, 11, 16)   # the current 10 bots and sizing started trading this day; older trades ran other rules
REVIEW_DAY = date(2027, 1, 15)      # the agreed January review of the paper test
PRIOR_PCT = {"drive": 4.0, "orb": 0.0}   # 2-year backtest, % gained per trade on the option: open-drive about +4%, opening-range about break-even
PRIOR_WEIGHT = 30                   # the backtest counts as 30 trades of evidence; past 30 live trades the live results lead
SAME_DAY_LINK = 0.7                 # trades on the same day move together: the bots all bet at 09:45 on related funds, so a
                                    # reversal day hurts them all at once (Nov 18, Nov 23). 0 would treat trades as independent.
OVERSHOOT = 0.06                    # what trades already open can still lose after the daily-loss stop (share of the account)
ENGINE_CONFIG = OB / "data" / "native" / "config.json"


def sizing(path: Path = ENGINE_CONFIG) -> dict:
    """The engine's live limits: either shares of the account (they grow with it) or fixed dollars."""
    r = (_json(path) or {}).get("risk") or {}
    f = lambda k, d=None: float(r[k]) if r.get(k) not in (None, "") else d
    return {"trade_pct": f("max_premium_per_position_pct"), "open_pct": f("max_exposure_account_pct"),
            "stop_pct": f("max_daily_loss_pct"), "trade_cap": f("max_premium_per_position", 1500.0),
            "open_cap": f("max_exposure_account", 4200.0), "stop_cap": f("max_daily_loss", 600.0),
            "max_contracts": int(r.get("max_contracts_per_order") or 10)}
_proj_cache: dict = {}


def trading_days_between(a: date, b: date) -> int:
    """Trading days after a, up to and including b."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += trading_day(d)
    return n


def _poisson(rng, lam: float) -> int:
    import math
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def projection(trips: list, now: datetime, horizon_days: int = 20, paths: int = 4000, equity: float = 0.0,
               limits: Optional[dict] = None) -> dict:
    """Profit projection for the next `horizon_days` trading days and to the January review.

    Per strategy family (open-drive, opening-range) the live % results per trade are blended with the 2-year backtest
    (PRIOR_PCT, weighted as PRIOR_WEIGHT trades), keeping the live spread of wins and losses. Then a Monte Carlo of daily
    trading: trades per day at the live rate, each trade sized the way the engine sizes it now (whole contracts at the
    live contract prices, within the per-trade, open-at-once and contract limits; when the limits are shares of the account
    they grow or shrink with it every day), each day's total floored at the daily-loss stop. Trades on one day share a common draw (SAME_DAY_LINK),
    so good and bad days come in whole days the way they do live. Returns the median and the 10th-90th percentile range."""
    import random
    import statistics
    live = [t for t in trips if t["opened"].date() >= LINEUP_START]
    today = now.date()
    done_days = trading_days_between(LINEUP_START - timedelta(days=1), today if now.time() >= time(16, 0) else today - timedelta(days=1))
    lim = limits or sizing()
    key = (today.isoformat(), len(live), done_days, horizon_days, round(equity), tuple(sorted(lim.items())))
    if key in _proj_cache:
        return _proj_cache[key]
    fam = lambda t: "drive" if t["bot"].endswith("-drive") else "orb"
    groups = {}
    for g in PRIOR_PCT:
        pcts = [t["pct"] for t in live if fam(t) == g]
        n = len(pcts)
        live_mean = statistics.fmean(pcts) if pcts else PRIOR_PCT[g]
        blended = (n * live_mean + PRIOR_WEIGHT * PRIOR_PCT[g]) / (n + PRIOR_WEIGHT)
        # with fewer than 5 trades there is no usable spread yet: borrow the other family's spread
        groups[g] = {"n": n, "live_mean": live_mean, "blended": blended, "pcts": pcts}
    pool = [t["pct"] for t in live] or [-30.0, 40.0]
    for g, v in groups.items():
        src = v["pcts"] if len(v["pcts"]) >= 5 else pool
        mean = statistics.fmean(src)
        v["samples"] = sorted(x - mean + v["blended"] for x in src)   # keep the spread, move the average to the blended one
    prices = [t["entry"] * 100 for t in live if t["entry"]] or [800.0]       # what one contract cost, live
    grows = lim["trade_pct"] is not None
    start_equity = equity or 5000.0
    rate = len(live) / done_days if done_days else 2.0
    share = groups["drive"]["n"] / len(live) if live else 0.7
    review_days = trading_days_between(today, REVIEW_DAY)
    import zlib
    rng = random.Random(zlib.crc32(repr(key).encode()))   # same day + same trades = the same projection, even after a restart

    import math
    link, rest = SAME_DAY_LINK, math.sqrt(1 - SAME_DAY_LINK ** 2)

    def draw(sorted_samples: list, u: float) -> float:
        """The u-quantile of a family's trade results (linear between samples), so the average stays the blended one."""
        x = u * (len(sorted_samples) - 1)
        i = int(x)
        j = min(i + 1, len(sorted_samples) - 1)
        return sorted_samples[i] + (sorted_samples[j] - sorted_samples[i]) * (x - i)

    # how sure we are of the average itself: the standard error of a mean from (live trades + the backtest's weight)
    spread = statistics.pstdev(pool) if len(pool) > 1 else 40.0
    edge_doubt = spread / math.sqrt(len(live) + PRIOR_WEIGHT)

    def caps(eq: float) -> tuple:
        if grows:
            return lim["trade_pct"] * eq, (lim["open_pct"] or 1.0) * eq, ((lim["stop_pct"] or 1.0) + OVERSHOOT) * eq
        return lim["trade_cap"], lim["open_cap"], lim["stop_cap"] + OVERSHOOT * start_equity

    costs_seen = []

    def run(days: int) -> list:
        out = []
        for _ in range(paths):
            eq, edge = start_equity, rng.gauss(0, edge_doubt)          # this path's own "true" edge, off by a typical error
            for _ in range(days):
                trade_cap, room, floor = caps(eq)
                day, z = 0.0, rng.gauss(0, 1)                      # the day's shared luck
                for _ in range(_poisson(rng, rate)):
                    price = rng.choice(prices)
                    qty = min(lim["max_contracts"], int(min(trade_cap, room) // price))
                    if qty <= 0:
                        continue
                    cost = qty * price
                    room -= cost
                    if len(costs_seen) < 5000:
                        costs_seen.append(cost)
                    g = "drive" if rng.random() < share else "orb"
                    u = 0.5 * (1 + math.erf((link * z + rest * rng.gauss(0, 1)) / math.sqrt(2)))
                    day += (draw(groups[g]["samples"], u) + edge) / 100 * cost
                eq += max(day, -floor)
            out.append(eq - start_equity)
        out.sort()
        return out

    def summary(vals: list) -> dict:
        q = lambda f: round(vals[min(len(vals) - 1, int(f * len(vals)))])
        return {"median": q(0.5), "low": q(0.10), "high": q(0.90), "chance_up": round(sum(v > 0 for v in vals) / len(vals) * 100)}

    wins = sum(t["pnl"] > 0 for t in live)
    win_amt = [t["pnl"] for t in live if t["pnl"] > 0]
    loss_amt = [t["pnl"] for t in live if t["pnl"] <= 0]
    res = {"trades": len(live), "win_rate": round(wins / len(live) * 100) if live else None,
           "avg_win": round(statistics.fmean(win_amt)) if win_amt else None, "avg_loss": round(statistics.fmean(loss_amt)) if loss_amt else None,
           "per_trade_pct": round(share * groups["drive"]["blended"] + (1 - share) * groups["orb"]["blended"], 1),
           "trades_per_day": round(rate, 1), "horizon_days": horizon_days,
           "next": summary(run(horizon_days)), "review_days": review_days, "grows": grows,
           "trade_pct": round(lim["trade_pct"] * 100) if grows else None,
           "review": summary(run(review_days)) if review_days else None, "live_weight": round(len(live) / (len(live) + PRIOR_WEIGHT) * 100),
           "edge_doubt": round(edge_doubt, 1), "cost": round(statistics.median(costs_seen)) if costs_seen else None}
    _proj_cache.clear()
    _proj_cache[key] = res
    return res


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
            "strategies": strategies, "projection": projection(trips, now, equity=equity)}
