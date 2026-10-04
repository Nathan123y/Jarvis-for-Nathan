"""Trend + momentum rotation across a handful of broad ETFs.

The rule, in plain words
    Look at six large, liquid funds: US large companies (SPY), US tech-heavy
    (QQQ), US small companies (IWM), developed overseas markets (EFA), long US
    government bonds (TLT) and gold (GLD).
    A fund is *eligible* only if it is trading above its 200-day average AND is
    up over the last 126 trading days (about six months).
    Of the eligible funds, hold the strongest three, an equal share each.
    Whatever there is no eligible fund for stays in cash. In a broad sell-off
    that can mean mostly or entirely cash, which is the point of the rule.

Why this and not something cleverer: it is simple enough to audit by eye, it
trades rarely (so costs and the day-trading rules never matter), it is a
long-studied idea rather than something fitted to recent prices, and every
number below was fixed before any result was seen. It is not a promise of
profit; `backtest` exists so you can see how it would have behaved, including
the bad years, next to simply holding SPY.

Everything here is a pure function over plain lists, so it is easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

UNIVERSE = ("SPY", "QQQ", "IWM", "EFA", "TLT", "GLD")
BENCHMARK = "SPY"


@dataclass(frozen=True)
class StrategyConfig:
    universe: tuple = UNIVERSE
    trend_days: int = 200
    momentum_days: int = 126
    hold: int = 3

    @property
    def min_history(self) -> int:
        return max(self.trend_days, self.momentum_days) + 1


# ── signals ───────────────────────────────────────────────────────────────────
def explain(closes: dict[str, list[float]], cfg: StrategyConfig = StrategyConfig()) -> list[dict]:
    """One row per fund: price, 200-day average, 6-month change, and a status label."""
    rows = []
    for symbol in cfg.universe:
        series = closes.get(symbol) or []
        if len(series) < cfg.min_history:
            rows.append({"symbol": symbol, "status": "not_enough_history",
                         "price": series[-1] if series else None, "sma": None, "momentum": None})
            continue
        price = series[-1]
        sma = sum(series[-cfg.trend_days:]) / cfg.trend_days
        momentum = price / series[-1 - cfg.momentum_days] - 1.0
        if price <= sma:
            status = "below_trend"
        elif momentum <= 0:
            status = "negative_momentum"
        else:
            status = "eligible"
        rows.append({"symbol": symbol, "status": status, "price": price,
                     "sma": sma, "momentum": momentum})
    return rows


def target_weights(closes: dict[str, list[float]],
                   cfg: StrategyConfig = StrategyConfig()) -> dict[str, float]:
    """{symbol: share of the account}. Shares sum to at most 1; the rest is cash."""
    eligible = [r for r in explain(closes, cfg) if r["status"] == "eligible"]
    eligible.sort(key=lambda r: (-r["momentum"], r["symbol"]))
    chosen = eligible[:cfg.hold]
    return {r["symbol"]: 1.0 / cfg.hold for r in chosen}


# ── schedule ──────────────────────────────────────────────────────────────────
def period_changed(previous: date, current: date, mode: str) -> bool:
    if mode == "daily":
        return current != previous
    if mode == "monthly":
        return (current.year, current.month) != (previous.year, previous.month)
    return current.isocalendar()[:2] != previous.isocalendar()[:2]      # weekly


def due_for_rebalance(last: Optional[str], today: date, mode: str = "weekly") -> bool:
    """True on the first trading day of a new week (or month), and on the very first run."""
    if not last:
        return True
    try:
        return period_changed(date.fromisoformat(last), today, mode)
    except ValueError:
        return True


# ── backtest ──────────────────────────────────────────────────────────────────
def _metrics(curve: list[tuple[str, float]]) -> dict:
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


def _by_year(curve: list[tuple[str, float]]) -> dict[int, float]:
    last_of_year: dict[int, float] = {}
    first = curve[0][1]
    for day, value in curve:
        last_of_year[int(day[:4])] = value
    out, previous = {}, first
    for year in sorted(last_of_year):
        out[year] = last_of_year[year] / previous - 1.0
        previous = last_of_year[year]
    return out


def backtest(series: dict[str, list[tuple[str, float]]], cfg: StrategyConfig = StrategyConfig(),
             *, rebalance: str = "weekly", slippage_bps: float = 2.0,
             start_equity: float = 100_000.0, benchmark: str = BENCHMARK) -> dict:
    """Replay the rule over history and compare it with holding the benchmark.

    A decision uses closing prices up to and including day t and is held for the
    move from t to t+1, so no day ever trades on prices it could not have seen.
    `slippage_bps` is charged on every dollar bought or sold.
    """
    needed = [s for s in cfg.universe if s in series] + ([benchmark] if benchmark not in cfg.universe else [])
    if len(needed) < len(cfg.universe) or any(not series.get(s) for s in needed):
        raise ValueError("Price history is missing for some funds.")
    common = set.intersection(*[{d for d, _ in series[s]} for s in needed])
    dates = sorted(common)
    if len(dates) < cfg.min_history + 20:
        raise ValueError("Not enough shared price history to run a backtest.")
    px = {s: {d: c for d, c in series[s]} for s in needed}
    paths = {s: [px[s][d] for d in dates] for s in needed}
    slip = slippage_bps / 10_000.0

    first = cfg.min_history - 1
    cash, held = start_equity, {s: 0.0 for s in cfg.universe}
    curve = [(dates[first], start_equity)]
    last_reb, rebalances, invested_days, tracked_days = None, 0, 0, 0
    for t in range(first, len(dates) - 1):
        if last_reb is None or period_changed(last_reb, date.fromisoformat(dates[t]), rebalance):
            history = {s: paths[s][:t + 1] for s in cfg.universe}
            weights = target_weights(history, cfg)
            equity = cash + sum(held.values())
            wanted = {s: weights.get(s, 0.0) * equity for s in cfg.universe}
            turnover = sum(abs(wanted[s] - held[s]) for s in cfg.universe)
            if turnover > 1e-6:
                rebalances += 1
            equity -= turnover * slip
            held = {s: weights.get(s, 0.0) * equity for s in cfg.universe}
            cash = equity - sum(held.values())
            last_reb = date.fromisoformat(dates[t])
        for s in cfg.universe:
            held[s] *= paths[s][t + 1] / paths[s][t]
        tracked_days += 1
        invested_days += 1 if sum(held.values()) > 1e-9 else 0
        curve.append((dates[t + 1], cash + sum(held.values())))

    base_close = paths[benchmark][first]
    bench_curve = [(dates[i], start_equity * paths[benchmark][i] / base_close)
                   for i in range(first, len(dates))]
    return {"start": curve[0][0], "end": curve[-1][0], "rebalance": rebalance,
            "slippage_bps": slippage_bps, "rebalances": rebalances,
            "time_invested": invested_days / tracked_days if tracked_days else 0.0,
            "strategy": _metrics(curve), "benchmark": _metrics(bench_curve),
            "strategy_by_year": _by_year(curve), "benchmark_by_year": _by_year(bench_curve)}
