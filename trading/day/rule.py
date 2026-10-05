"""Opening-range breakout, long only, flat every night.

The rule, in plain words (SPY and QQQ, regular hours, New York time)
    1. Watch the first 15 minutes (9:30 to 9:45). Note the highest and lowest price.
    2. After 9:45, the first time a one-minute candle CLOSES above that high, buy.
       Only one buy per fund per day, and a candle that starts at 2:00 pm or later cannot
       trigger one.
    3. A protective stop sits at the low of the opening range. If it is hit, you are out.
    4. Size so that being stopped out costs about a quarter of one percent of the account,
       and never put more than a quarter of the account into one fund.
    5. Whatever is still held at 3:45 pm is sold. Nothing is ever held overnight,
       nothing is ever sold short, and there is no borrowing.
    6. Sit out days with an early close, a missing or patchy opening range, an opening
       range under 0.05% of the price (no room to be wrong) or a stop more than 1.5% away.

Why this rule: it is a widely studied, easy-to-audit pattern, and every number above was
fixed from round textbook values before any result was seen. That is not evidence that it
works. Day trading is mostly a contest against costs and noise, and `backtest` exists so
you can see how it would have behaved, bad stretches included, next to simply holding SPY.

Everything here is a pure function over plain tuples. A bar is
(minute_of_day_in_New_York, open, high, low, close).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo

from trading.broker import parse_ts

EASTERN = ZoneInfo("America/New_York")
SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
MIN_BACKTEST_DAYS = 60


@dataclass(frozen=True)
class DayConfig:
    symbols: tuple = ("SPY", "QQQ")
    range_minutes: int = 15
    last_entry_minute: int = 14 * 60         # a candle starting at 2:00 pm or later cannot trigger a buy
    flatten_before_close: int = 15           # sell what is left 15 minutes before the close
    risk_per_trade: float = 0.0025           # lose about 0.25% of the account if the stop is hit
    max_position_pct: float = 0.25           # never more than 25% of the account in one fund
    min_range_pct: float = 0.0005
    max_risk_pct: float = 0.015
    min_range_bars: int = 10                 # of the 15 opening minutes, at least this many must exist
    daily_loss_halt: float = 0.01            # live only: stop for the day if the account is down 1%
    fresh_seconds: int = 90                  # live only: a breakout older than this is not chased

    @property
    def flatten_minute(self) -> int:
        return SESSION_CLOSE - self.flatten_before_close

    @property
    def range_end(self) -> int:
        return SESSION_OPEN + self.range_minutes


STATUS_TEXT = {
    "traded": "bought on a breakout",
    "enter": "breakout just happened: buy now",
    "too_early": "still building the opening range",
    "no_session": "no full session of prices",
    "no_opening_range": "opening range missing or patchy",
    "no_breakout": "no breakout yet",
    "breakout_stale": "breakout already happened; not chasing it",
    "range_too_narrow": "opening range too narrow",
    "stop_too_far": "stop would be too far away",
    "too_small": "stop too wide to buy even one share within the limits",
    "no_next_bar": "breakout on the last candle",
    "late": "no breakout before 2:00 pm",
}
# Outcomes after which nothing more can happen for that fund today.
FINAL = {"traded", "no_session", "no_opening_range", "breakout_stale", "range_too_narrow",
         "stop_too_far", "too_small", "no_next_bar", "late"}


# ── turning Alpaca's minute bars into New York trading days ───────────────────
def to_sessions(raw: list[tuple]) -> dict[str, list[tuple]]:
    """[(utc_time, o, h, l, c), ...] -> {new_york_date: [(minute, o, h, l, c), ...]}, regular hours only."""
    days: dict[str, list[tuple]] = {}
    for stamp, o, h, l, c in raw:
        try:
            if not 13 <= int(stamp[11:13]) <= 20:       # cheap filter: regular hours are 13:30-21:00 UTC
                continue
            local = parse_ts(stamp).astimezone(EASTERN)
        except (ValueError, IndexError):
            continue
        minute = local.hour * 60 + local.minute
        if SESSION_OPEN <= minute < SESSION_CLOSE:
            days.setdefault(local.date().isoformat(), []).append((minute, o, h, l, c))
    for bars in days.values():
        bars.sort(key=lambda b: b[0])
    return days


def is_full_session(bars: list[tuple]) -> bool:
    """A normal 9:30-4:00 day. Early-close days and days with big data holes are not."""
    return bool(bars) and bars[0][0] <= SESSION_OPEN + 10 and bars[-1][0] >= SESSION_CLOSE - 30


# ── turning a list of (day, account value) into the headline numbers ──────────
def curve_metrics(curve: list[tuple[str, float]]) -> dict:
    start_day, start_value = curve[0]
    end_day, end_value = curve[-1]
    years = max((date.fromisoformat(end_day) - date.fromisoformat(start_day)).days / 365.25, 1e-9)
    peak, worst = start_value, 0.0
    for _, value in curve:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    total = end_value / start_value - 1.0
    cagr = (end_value / start_value) ** (1.0 / years) - 1.0 if years >= 0.5 else total
    return {"total_return": total, "cagr": cagr, "max_drawdown": worst, "end_value": end_value}


def curve_by_year(curve: list[tuple[str, float]]) -> dict[int, float]:
    last_of_year: dict[int, float] = {}
    first = curve[0][1]
    for day, value in curve:
        last_of_year[int(day[:4])] = value
    out, previous = {}, first
    for year in sorted(last_of_year):
        out[year] = last_of_year[year] / previous - 1.0
        previous = last_of_year[year]
    return out


# ── the rule ──────────────────────────────────────────────────────────────────
def opening_range(bars: list[tuple], cfg: DayConfig = DayConfig()) -> Optional[tuple[float, float]]:
    inside = [b for b in bars if SESSION_OPEN <= b[0] < cfg.range_end]
    if len(inside) < cfg.min_range_bars:
        return None
    return max(b[2] for b in inside), min(b[3] for b in inside)


def find_breakout(bars: list[tuple], high: float, cfg: DayConfig = DayConfig()) -> Optional[int]:
    """Index of the first candle after the opening range that closes above its high."""
    for index, bar in enumerate(bars):
        if bar[0] < cfg.range_end:
            continue
        if bar[0] >= cfg.last_entry_minute:
            return None
        if bar[4] > high:
            return index
    return None


def size_entry(close: float, high: float, low: float, equity: float,
               cfg: DayConfig = DayConfig()) -> tuple[Optional[dict], str]:
    """Whole shares and the stop price for a breakout candle that closed at `close`."""
    if close <= 0 or equity <= 0:
        return None, "too_small"
    if (high - low) / close < cfg.min_range_pct:
        return None, "range_too_narrow"
    stop = round(low, 2)
    risk = close - stop
    if risk <= 0 or risk / close > cfg.max_risk_pct:
        return None, "stop_too_far"
    qty = min(math.floor(equity * cfg.risk_per_trade / risk),
              math.floor(equity * cfg.max_position_pct / close))
    if qty < 1:
        return None, "too_small"
    return {"qty": qty, "stop": stop, "risk": risk}, "ok"


def live_signal(bars: list[tuple], cfg: DayConfig, equity: float, now_seconds: float) -> dict:
    """What the rule says right now for one fund, from today's candles.

    `now_seconds` is seconds since midnight in New York. Only candles that have finished are
    used (a candle starting at minute m finishes at (m+1)*60). A breakout is acted on only if
    it just happened: the backtest buys the candle after the signal, so buying one that is
    minutes old would be a different, untested trade."""
    done = [b for b in bars if (b[0] + 1) * 60 <= now_seconds]
    if now_seconds < cfg.range_end * 60:
        return {"status": "too_early"}
    rng = opening_range(done, cfg)
    if rng is None:
        return {"status": "no_opening_range"}
    high, low = rng
    out = {"range": (high, low)}
    index = find_breakout(done, high, cfg)
    if index is None:
        late = now_seconds >= cfg.last_entry_minute * 60
        return {**out, "status": "late" if late else "no_breakout"}
    signal = done[index]
    sized, why = size_entry(signal[4], high, low, equity, cfg)
    out.update(signal_minute=signal[0], signal_close=signal[4])
    if sized is None:
        return {**out, "status": why}
    if now_seconds - (signal[0] + 1) * 60 > cfg.fresh_seconds:
        return {**out, "status": "breakout_stale", **sized}
    return {**out, "status": "enter", **sized}


# ── replaying the rule on past candles ────────────────────────────────────────
def simulate_day(bars: list[tuple], cfg: DayConfig, equity: float, slip: float) -> dict:
    """What the rule does with one fund on one finished day. `slip` is a fraction of price
    charged against every fill. Buys fill at the open of the candle after the signal; a stop
    fills at its price (or at the open if the price gapped through it); if a candle could have
    touched the stop, the stop is assumed to be hit."""
    if not is_full_session(bars):
        return {"status": "no_session"}
    rng = opening_range(bars, cfg)
    if rng is None:
        return {"status": "no_opening_range"}
    high, low = rng
    index = find_breakout(bars, high, cfg)
    if index is None:
        return {"status": "no_breakout", "range": rng}
    sized, why = size_entry(bars[index][4], high, low, equity, cfg)
    if sized is None:
        return {"status": why, "range": rng}
    if index + 1 >= len(bars):
        return {"status": "no_next_bar", "range": rng}
    qty, stop = sized["qty"], sized["stop"]
    entry = bars[index + 1][1] * (1.0 + slip)
    exit_price, reason, exit_minute = bars[-1][4] * (1.0 - slip), "data_end", bars[-1][0]
    for minute, o, h, l, c in bars[index + 1:]:
        if minute >= cfg.flatten_minute:
            exit_price, reason, exit_minute = o * (1.0 - slip), "time", minute
            break
        if l <= stop:
            exit_price, reason, exit_minute = min(stop, o) * (1.0 - slip), "stop", minute
            break
    return {"status": "traded", "range": rng, "trade": {
        "qty": qty, "entry": entry, "exit": exit_price, "stop": stop, "reason": reason,
        "pnl": (exit_price - entry) * qty, "entered": bars[index + 1][0], "exited": exit_minute}}


def backtest(sessions: dict[str, dict[str, list]], cfg: DayConfig = DayConfig(), *,
             slippage_bps: float = 2.0, start_equity: float = 100_000.0,
             benchmark_daily: Optional[list[tuple[str, float]]] = None) -> dict:
    """Replay the rule over past days and compare it with holding SPY.

    `sessions` is {symbol: {day: bars}}. Each day both funds are sized from the same
    start-of-day account value; profits and losses compound from day to day.
    `benchmark_daily` is optional dividend-adjusted SPY daily closes [(day, close)], which
    makes the hold-SPY column fair; without it, raw candle closes are used (no dividends)."""
    if "SPY" not in sessions:
        raise ValueError("SPY prices are needed for the comparison.")
    days = sorted(d for d, bars in sessions["SPY"].items() if is_full_session(bars))
    if len(days) < MIN_BACKTEST_DAYS:
        raise ValueError(f"Only {len(days)} full trading days of prices came back; need at least "
                         f"{MIN_BACKTEST_DAYS}.")
    slip = slippage_bps / 10_000.0
    equity = start_equity
    curve = [(days[0], equity)]
    trades: list[dict] = []
    skipped: dict[str, int] = {}
    for day in days:
        day_pnl = 0.0
        for symbol in cfg.symbols:
            bars = sessions.get(symbol, {}).get(day)
            if not bars:
                continue
            result = simulate_day(bars, cfg, equity, slip)
            if result["status"] != "traded":
                skipped[result["status"]] = skipped.get(result["status"], 0) + 1
                continue
            trade = {**result["trade"], "symbol": symbol, "day": day}
            trades.append(trade)
            day_pnl += trade["pnl"]
        equity += day_pnl
        curve.append((day, equity))

    daily = dict(benchmark_daily or [])
    spy_sessions = sessions["SPY"]
    before = [d for d in sorted(daily) if d < days[0]]
    if daily and before and all(day in daily for day in days):
        base, closes = daily[before[-1]], [daily[d] for d in days]
        adjusted = True
    else:
        base, closes = spy_sessions[days[0]][0][1], [spy_sessions[d][-1][4] for d in days]
        adjusted = False
    bench = [(days[0], start_equity)] + [(d, start_equity * c / base) for d, c in zip(days, closes)]

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    count = len(trades)
    gross_loss = -sum(losses)
    return {
        "start": days[0], "end": days[-1], "days": len(days), "slippage_bps": slippage_bps,
        "trades": count,
        "days_traded": len({t["day"] for t in trades}),
        "win_rate": (len(wins) / count) if count else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "expectancy": (sum(t["pnl"] for t in trades) / count) if count else None,
        "stops": sum(1 for t in trades if t["reason"] == "stop"),
        "time_exits": sum(1 for t in trades if t["reason"] == "time"),
        "avg_minutes_held": (sum(t["exited"] - t["entered"] for t in trades) / count) if count else None,
        "skipped": skipped, "benchmark_adjusted": adjusted,
        "strategy": curve_metrics(curve), "benchmark": curve_metrics(bench),
        "strategy_by_year": curve_by_year(curve), "benchmark_by_year": curve_by_year(bench),
    }


# ── turning the day's fills into a result ─────────────────────────────────────
def summarize_fills(orders: list[dict], symbols: tuple, day: str) -> list[dict]:
    """Per-fund result for `day` from Alpaca's filled orders: [{symbol, pnl, qty, bought, sold,
    exit}]. Gross of any fees; a fund that is not flat again is left out (the next pass catches it)."""
    out = []
    for symbol in symbols:
        fills = [o for o in orders if o["symbol"] == symbol and str(o.get("filled_at", ""))[:10] >= day[:10]]
        buys = [o for o in fills if o["side"] == "buy"]
        sells = [o for o in fills if o["side"] == "sell"]
        bought_qty, sold_qty = sum(o["qty"] for o in buys), sum(o["qty"] for o in sells)
        if not buys or abs(bought_qty - sold_qty) > 1e-6:
            continue
        bought = sum(o["qty"] * o["price"] for o in buys)
        sold = sum(o["qty"] * o["price"] for o in sells)
        kinds = {o["type"] for o in sells}
        out.append({"symbol": symbol, "qty": bought_qty, "bought": bought, "sold": sold,
                    "pnl": sold - bought,
                    "exit": "stop" if "stop" in kinds or "stop_limit" in kinds else "time"})
    return out
