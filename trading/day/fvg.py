"""Fair-value-gap entry after an opening-range break, long only, flat every night.

The rule, in plain words (SPY and QQQ, regular hours, New York time). It follows the
"15-minute / 5-minute" day-trading entry from "The Best FVG Trading Strategy" (Jesse Rogers,
Casper Trading), with every number fixed here before any result was seen:
    1. Mark the high and low of the first 15 minutes (9:30 to 9:45), from one-minute candles.
    2. Group the one-minute candles into five-minute candles starting at 9:30. A five-minute
       candle needs at least 3 of its 5 minutes, or it cannot be part of a pattern (the free
       feed is one exchange and misses minutes; a missing candle must never fake a gap).
    3. Look for three back-to-back five-minute candles c1, c2, c3 where c2 starts at 9:45 or
       later and c3 has closed:
         up:   c3's low is above c1's high (a gap), c2 closed up and its high went above
               the opening-range high.
         down: the mirror (c3's high below c1's low, c2 closed down, its low under the range low).
    4. The first such gap of the day decides. If it points down, that fund is left alone for
       the day (no shorting). The pattern must be complete by 11:00.
    5. Buy with a limit order at the top of the gap (c3's low) as soon as c3 closes. It fills
       if the price comes back down into the gap. Not filled by 11:00: cancelled. Price under
       the stop before it fills: cancelled.
    6. Stop one cent under c1's low; target twice the risk above the entry. Nothing else moves
       them. Whatever is still open 15 minutes before the close is sold.
    7. One trade per fund per day. Skip a risk under 0.05% or over 1.5% of the price.
    8. Size so a stop costs about 0.25% of the account, never more than 25% of it in one fund.

Pure functions over (minute_of_day_in_New_York, open, high, low, close) tuples. No AI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional

from trading.day.rule import (SESSION_CLOSE, SESSION_OPEN, DayConfig, curve_by_year, curve_metrics,
                              is_full_session, opening_range)

CANDLE = 5                                  # minutes in a pattern candle


@dataclass(frozen=True)
class FvgConfig:
    symbols: tuple = ("SPY", "QQQ")
    range_minutes: int = 15
    min_range_bars: int = 10
    min_candle_bars: int = 3                 # of a five-minute candle's 5 minutes, at least this many must exist
    first_c2_minute: int = 9 * 60 + 45       # the middle candle starts at 9:45 or later
    cutoff_minute: int = 11 * 60             # pattern complete and order filled by 11:00, or nothing
    stop_offset: float = 0.01                # stop one cent under c1's low
    reward_risk: float = 2.0                 # target = entry + 2 x risk
    min_risk_pct: float = 0.0005
    max_risk_pct: float = 0.015
    risk_per_trade: float = 0.0025
    max_position_pct: float = 0.25
    flatten_before_close: int = 15
    settle_seconds: int = 20                 # live only: wait this long after a candle ends for its last minute to arrive
    # The three pre-declared robustness checks (never the headline settings):
    entry_at: str = "edge"                   # "edge" (c3's low) or "mid" (middle of the gap)
    c2_must: str = "trade"                   # "trade" through the level, or "close" beyond it
    fill_on: str = "through"                 # "through" (a cent past the price) or "touch"

    @property
    def flatten_minute(self) -> int:
        return SESSION_CLOSE - self.flatten_before_close

    @property
    def range_end(self) -> int:
        return SESSION_OPEN + self.range_minutes


def fvg_config(day_cfg: DayConfig) -> FvgConfig:
    """The fvg rule with the size settings (and nothing else) taken from the command line's DayConfig."""
    return FvgConfig(risk_per_trade=day_cfg.risk_per_trade, max_position_pct=day_cfg.max_position_pct)


STATUS_TEXT = {
    "too_early": "still building the opening range",
    "no_session": "no full session of prices",
    "no_opening_range": "opening range missing or patchy",
    "no_pattern": "no fair value gap yet",
    "no_pattern_by_cutoff": "no fair value gap by 11:00",
    "bearish_stand_aside": "first gap pointed down: standing aside",
    "risk_too_small": "stop too close to the entry",
    "stop_too_far": "stop would be too far away",
    "too_small": "stop too wide to buy even one share within the limits",
    "already_played_out": "the price already came back to the gap; not chasing it",
    "place": "gap found: place the limit buy",
    "never_filled": "limit buy not filled by 11:00",
    "invalidated": "price fell under the stop before the buy filled",
    "traded": "bought in the gap",
}
FINAL = {"no_session", "no_opening_range", "no_pattern_by_cutoff", "bearish_stand_aside", "risk_too_small",
         "stop_too_far", "too_small", "already_played_out"}
BAD_DATA = {"no_session", "no_opening_range"}


# ── five-minute candles ──────────────────────────────────────────────────────
def five_minute(bars: list[tuple], cfg: FvgConfig = FvgConfig()) -> dict[int, tuple]:
    """{start_minute: (start, open, high, low, close)} for the valid five-minute candles, aligned to 9:30.
    A candle with fewer than `min_candle_bars` one-minute candles is left out entirely."""
    buckets: dict[int, list[tuple]] = {}
    for bar in bars:
        if SESSION_OPEN <= bar[0] < SESSION_CLOSE:
            start = SESSION_OPEN + (bar[0] - SESSION_OPEN) // CANDLE * CANDLE
            buckets.setdefault(start, []).append(bar)
    out = {}
    for start, group in buckets.items():
        if len(group) < cfg.min_candle_bars:
            continue
        group.sort(key=lambda b: b[0])
        out[start] = (start, group[0][1], max(b[2] for b in group), min(b[3] for b in group), group[-1][4])
    return out


def find_gap(candles: dict[int, tuple], rng: tuple[float, float], cfg: FvgConfig = FvgConfig()) -> Optional[dict]:
    """The first fair value gap of the day (ordered by c3's close) that broke the opening range,
    or None. {"direction": "up"|"down", "c1", "c2", "c3", "done": minute c3 closes}."""
    high, low = rng
    for start in sorted(candles):
        c3, c2, c1 = candles[start], candles.get(start - CANDLE), candles.get(start - 2 * CANDLE)
        if c1 is None or c2 is None or c2[0] < cfg.first_c2_minute:
            continue
        if start + CANDLE > cfg.cutoff_minute:
            return None
        through_up = (c2[4] if cfg.c2_must == "close" else c2[2]) > high
        through_down = (c2[4] if cfg.c2_must == "close" else c2[3]) < low
        if c3[3] > c1[2] and c2[4] > c2[1] and through_up:
            return {"direction": "up", "c1": c1, "c2": c2, "c3": c3, "done": start + CANDLE}
        if c3[2] < c1[3] and c2[4] < c2[1] and through_down:
            return {"direction": "down", "c1": c1, "c2": c2, "c3": c3, "done": start + CANDLE}
    return None


def levels(gap: dict, cfg: FvgConfig = FvgConfig()) -> dict:
    """Entry, stop and target, in cents, for the trade the gap points to (a short for a down gap)."""
    c1, c3 = gap["c1"], gap["c3"]
    if gap["direction"] == "up":
        edge, far = c3[3], c1[2]
        entry = round(edge if cfg.entry_at == "edge" else (edge + far) / 2.0, 2)
        stop = round(c1[3] - cfg.stop_offset, 2)
        target = round(entry + cfg.reward_risk * (entry - stop), 2)
    else:
        edge, far = c3[2], c1[3]
        entry = round(edge if cfg.entry_at == "edge" else (edge + far) / 2.0, 2)
        stop = round(c1[2] + cfg.stop_offset, 2)
        target = round(entry - cfg.reward_risk * (stop - entry), 2)
    return {"entry": entry, "stop": stop, "target": target, "risk": round(abs(entry - stop), 4)}


def size(lv: dict, equity: float, cfg: FvgConfig = FvgConfig()) -> tuple[Optional[int], str]:
    entry, risk = lv["entry"], lv["risk"]
    if entry <= 0 or equity <= 0 or risk <= 0:
        return None, "too_small"
    if risk / entry < cfg.min_risk_pct:
        return None, "risk_too_small"
    if risk / entry > cfg.max_risk_pct:
        return None, "stop_too_far"
    qty = min(math.floor(equity * cfg.risk_per_trade / risk), math.floor(equity * cfg.max_position_pct / entry))
    return (qty, "ok") if qty >= 1 else (None, "too_small")


# ── live ─────────────────────────────────────────────────────────────────────
def live_signal(bars: list[tuple], cfg: FvgConfig, equity: float, now_seconds: float) -> dict:
    """What the rule says right now for one fund. "place" means: send the limit buy now.
    A five-minute candle counts as closed `settle_seconds` after it ends, so its last minute is in."""
    if now_seconds < cfg.range_end * 60:
        return {"status": "too_early"}
    done = [b for b in bars if (b[0] + 1) * 60 <= now_seconds]
    rng = opening_range(done, DayConfig(range_minutes=cfg.range_minutes, min_range_bars=cfg.min_range_bars))
    if rng is None:
        return {"status": "no_opening_range"}
    closed = {s: c for s, c in five_minute(done, cfg).items()
              if (s + CANDLE) * 60 + cfg.settle_seconds <= now_seconds}
    gap = find_gap(closed, rng, cfg)
    out: dict = {"range": rng}
    if gap is None:
        late = now_seconds >= cfg.cutoff_minute * 60 + cfg.settle_seconds
        return {**out, "status": "no_pattern_by_cutoff" if late else "no_pattern"}
    out["gap"] = gap
    if gap["direction"] == "down":
        return {**out, "status": "bearish_stand_aside"}
    lv = levels(gap, cfg)
    out.update(lv)
    qty, why = size(lv, equity, cfg)
    if qty is None:
        return {**out, "status": why}
    after = [b for b in done if b[0] >= gap["done"]]
    if any(b[3] <= lv["entry"] for b in after):
        return {**out, "status": "already_played_out"}
    if now_seconds >= cfg.cutoff_minute * 60:
        return {**out, "status": "no_pattern_by_cutoff"}
    return {**out, "status": "place", "qty": qty}


# ── replay ───────────────────────────────────────────────────────────────────
def _replay(bars: list[tuple], gap: dict, lv: dict, qty: int, cfg: FvgConfig, slip: float, long: bool) -> dict:
    """Fill the limit and run the trade to its stop, target or the end-of-day sale on one-minute candles.
    Conservative: the limit and the target need a trade a cent beyond them (unless fill_on is "touch");
    the bar that fills cannot also reach the target; a bar that could touch both stop and target is a stop."""
    entry, stop, target = lv["entry"], lv["stop"], lv["target"]
    step = 0.0 if cfg.fill_on == "touch" else 0.01
    sign = 1.0 if long else -1.0
    after = [b for b in bars if b[0] >= gap["done"]]
    filled_at = None
    for i, (minute, o, h, l, c) in enumerate(after):
        if minute >= cfg.cutoff_minute:
            return {"status": "never_filled"}
        fills = (l <= entry - step) if long else (h >= entry + step)
        if fills:
            filled_at = i
            break
        if (l <= stop) if long else (h >= stop):
            return {"status": "invalidated"}
    if filled_at is None:
        return {"status": "never_filled"}
    exit_price, reason, exit_minute = None, None, None
    for j, (minute, o, h, l, c) in enumerate(after[filled_at:]):
        if minute >= cfg.flatten_minute:
            exit_price, reason, exit_minute = o * (1.0 - sign * slip), "time", minute
            break
        if (l <= stop) if long else (h >= stop):
            worse = min(stop, o) if long else max(stop, o)
            exit_price, reason, exit_minute = worse * (1.0 - sign * slip), "stop", minute
            break
        if j > 0 and ((h >= target + step) if long else (l <= target - step)):
            exit_price, reason, exit_minute = target, "target", minute
            break
    if exit_price is None:
        last = after[-1]
        exit_price, reason, exit_minute = last[4] * (1.0 - sign * slip), "time", last[0]
    pnl = sign * (exit_price - entry) * qty
    return {"status": "traded", "trade": {
        "qty": qty, "entry": entry, "exit": exit_price, "stop": stop, "target": target, "reason": reason,
        "pnl": pnl, "r": sign * (exit_price - entry) / lv["risk"], "entered": after[filled_at][0],
        "exited": exit_minute, "side": "long" if long else "short"}}


def simulate_day(bars: list[tuple], cfg: FvgConfig, equity: float, slip: float) -> dict:
    """What the rule does with one fund on one finished day. A down gap also gets an
    `"info_short"` replay of the mirror short: information only, never traded here."""
    if not is_full_session(bars):
        return {"status": "no_session"}
    rng = opening_range(bars, DayConfig(range_minutes=cfg.range_minutes, min_range_bars=cfg.min_range_bars))
    if rng is None:
        return {"status": "no_opening_range"}
    gap = find_gap(five_minute(bars, cfg), rng, cfg)
    if gap is None:
        return {"status": "no_pattern", "range": rng}
    lv = levels(gap, cfg)
    qty, why = size(lv, equity, cfg)
    long = gap["direction"] == "up"
    if qty is None:
        return {"status": why if long else "bearish_stand_aside", "range": rng}
    result = _replay(bars, gap, lv, qty, cfg, slip, long)
    if long:
        return {**result, "range": rng, "gap": gap, **lv}
    return {"status": "bearish_stand_aside", "range": rng, "info_short": result}


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_loss = -sum(t["pnl"] for t in losses)
    streak = longest = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        longest = max(longest, streak)
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else None,
        "avg_win_r": sum(t["r"] for t in wins) / len(wins) if wins else None,
        "avg_loss_r": sum(t["r"] for t in losses) / len(losses) if losses else None,
        "expectancy_r": sum(t["r"] for t in trades) / n if n else None,
        "expectancy": sum(t["pnl"] for t in trades) / n if n else None,
        "profit_factor": sum(t["pnl"] for t in wins) / gross_loss if gross_loss > 0 else None,
        "targets": sum(1 for t in trades if t["reason"] == "target"),
        "stops": sum(1 for t in trades if t["reason"] == "stop"),
        "time_exits": sum(1 for t in trades if t["reason"] == "time"),
        "losing_streak": longest,
    }


def backtest(sessions: dict[str, dict[str, list]], cfg: FvgConfig = FvgConfig(), *,
             slippage_bps: float = 2.0, start_equity: float = 100_000.0,
             benchmark_daily: Optional[list[tuple[str, float]]] = None) -> dict:
    """Replay the rule over past days next to holding SPY, in the same shape as rule.backtest plus
    per-trade R numbers, results by month, skip reasons and the information-only short side."""
    from trading.day.rule import MIN_BACKTEST_DAYS
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
    shorts: list[dict] = []
    skipped: dict[str, int] = {}
    for day in days:
        day_pnl = 0.0
        for symbol in cfg.symbols:
            bars = sessions.get(symbol, {}).get(day)
            if not bars:
                skipped["no_session"] = skipped.get("no_session", 0) + 1
                continue
            result = simulate_day(bars, cfg, equity, slip)
            info = result.get("info_short")
            if info and info.get("status") == "traded":
                shorts.append({**info["trade"], "symbol": symbol, "day": day})
            if result["status"] != "traded":
                skipped[result["status"]] = skipped.get(result["status"], 0) + 1
                continue
            trade = {**result["trade"], "symbol": symbol, "day": day}
            trades.append(trade)
            day_pnl += trade["pnl"]
        equity += day_pnl
        curve.append((day, equity))

    daily = dict(benchmark_daily or [])
    before = [d for d in sorted(daily) if d < days[0]]
    if daily and before and all(day in daily for day in days):
        base, closes, adjusted = daily[before[-1]], [daily[d] for d in days], True
    else:
        spy = sessions["SPY"]
        base, closes, adjusted = spy[days[0]][0][1], [spy[d][-1][4] for d in days], False
    bench = [(days[0], start_equity)] + [(d, start_equity * c / base) for d, c in zip(days, closes)]

    by_month: dict[str, dict] = {}
    previous = start_equity
    for day, value in curve[1:]:
        month = by_month.setdefault(day[:7], {"trades": 0, "wins": 0, "r": 0.0, "start": previous})
        month["end"] = value
        previous = value
    for t in trades:
        month = by_month[t["day"][:7]]
        month["trades"] += 1
        month["wins"] += t["pnl"] > 0
        month["r"] += t["r"]
    for month in by_month.values():
        month["return"] = month.pop("end") / month.pop("start") - 1.0
    months = max(len(by_month), 1)
    return {
        "start": days[0], "end": days[-1], "days": len(days), "slippage_bps": slippage_bps,
        **_stats(trades), "trades_per_month": len(trades) / months, "months": len(by_month),
        "days_traded": len({t["day"] for t in trades}),
        "skipped": skipped, "benchmark_adjusted": adjusted, "by_month": by_month,
        "strategy": curve_metrics(curve), "benchmark": curve_metrics(bench),
        "strategy_by_year": curve_by_year(curve), "benchmark_by_year": curve_by_year(bench),
        "short_info": _stats(shorts), "trade_list": trades,
    }


SENSITIVITY = (("(a) entry at mid-gap", {"entry_at": "mid"}),
               ("(b) c2 must CLOSE beyond the level", {"c2_must": "close"}),
               ("(c) fills on touch, not trade-through", {"fill_on": "touch"}))


def sensitivity(sessions, cfg: FvgConfig = FvgConfig(), **kw) -> list[tuple[str, dict]]:
    return [(label, backtest(sessions, replace(cfg, **change), **kw)) for label, change in SENSITIVITY]
