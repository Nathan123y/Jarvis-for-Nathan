"""Day-trader results, always next to simply holding SPY, with trading costs estimated.

The practice account fills orders without charging the spread, fees or slippage that a real
account pays. A day trader trades often, so that gap matters far more than for the weekly
rule. This report therefore shows the result as Alpaca recorded it AND an estimate after
costs, and says plainly when the sample is too short to mean anything.
"""
from __future__ import annotations

from typing import Optional

from trading.report import MIN_TRADING_DAYS, _pct, _usd

COST_BPS = 2.0                 # assumed cost per side, as in the backtest


def _unique_trades(events: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for event in events:
        seen[(event.get("day"), event.get("symbol"))] = event
    return [seen[key] for key in sorted(seen, key=lambda k: (str(k[0]), str(k[1])))]


def build(state: dict, snapshots: list[dict], trade_events: list[dict], *,
          account: Optional[dict] = None, positions: Optional[dict] = None,
          spy_price: Optional[float] = None, cost_bps: float = COST_BPS) -> dict:
    seen = state.get("last_seen") or {}
    live = account is not None
    equity = account["equity"] if live else (seen.get("equity") or (snapshots[-1]["equity"] if snapshots else None))
    last_spy = next((s["spy"] for s in reversed(snapshots) if s.get("spy")), None)
    spy_now = spy_price or last_spy
    start_equity, start_spy = state.get("start_equity"), state.get("start_spy")
    started = bool(start_equity and start_spy)
    mine = (equity / start_equity - 1.0) if (started and equity) else None
    spy = (spy_now / start_spy - 1.0) if (started and spy_now) else None
    days = len({s.get("date") for s in snapshots if s.get("date")})

    curve = ([start_equity] if started else []) + [s["equity"] for s in snapshots
                                                     if isinstance(s.get("equity"), (int, float))]
    if equity:
        curve.append(equity)
    peak, worst = 0.0, 0.0
    for value in curve:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)

    trades = _unique_trades(trade_events)
    wins = [t["pnl"] for t in trades if t.get("pnl", 0) > 0]
    losses = [t["pnl"] for t in trades if t.get("pnl", 0) <= 0]
    gross = sum(t.get("pnl", 0.0) for t in trades)
    costs = sum((t.get("bought", 0.0) + t.get("sold", 0.0)) * cost_bps / 10_000.0 for t in trades)
    after_costs = (equity - costs) / start_equity - 1.0 if (started and equity) else None
    held = positions if positions is not None else {
        h["symbol"]: {"market_value": h.get("value", 0.0)} for h in seen.get("holdings", [])}

    report = {
        "started": started, "since": str(state.get("started_at", ""))[:10],
        "source": "live" if live else "recorded", "as_of": seen.get("t", ""),
        "equity": equity, "start_equity": start_equity, "return": mine, "spy_return": spy,
        "vs_spy": (mine - spy) if (mine is not None and spy is not None) else None,
        "return_after_costs": after_costs, "costs": costs,
        "vs_spy_after_costs": (after_costs - spy) if (after_costs is not None and spy is not None) else None,
        "trading_days": days, "max_drawdown": worst,
        "trades": len(trades), "win_rate": (len(wins) / len(trades)) if trades else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "stops": sum(1 for t in trades if t.get("exit") == "stop"), "gross_pnl": gross,
        "holding": sorted(held), "paused": bool(state.get("paused")),
    }
    report["verdict"] = verdict(report)
    return report


def verdict(report: dict) -> str:
    if not report["started"]:
        return "Nothing to judge yet: the first trading day has not happened."
    days, gap = report["trading_days"], report["vs_spy_after_costs"]
    if days < MIN_TRADING_DAYS:
        return (f"Too early to tell. {days} trading day{'s' if days != 1 else ''} is mostly luck in "
                f"either direction; give it at least {MIN_TRADING_DAYS}, and read the backtest for "
                "the longer view.")
    if gap is None:
        return "Not enough data to compare with SPY yet."
    if gap >= 0:
        return (f"Ahead of simply holding SPY by {gap * 100:.1f} points after estimated costs, over "
                f"{days} trading days. Still a short sample, and this was practice money.")
    return (f"Behind simply holding SPY by {-gap * 100:.1f} points after estimated costs, over "
            f"{days} trading days. Day trading is hard to beat after costs; that is a normal result.")


def render(report: dict) -> str:
    if not report["started"]:
        return "The day trader has not started yet. Run:  python3 -m trading.day run"
    pf = report["profit_factor"]
    lines = [
        f"Practice day-trading account since {report['since']}  ({report['source']} numbers"
        + (f", as of {report['as_of']}" if report["as_of"] else "") + ")",
        f"  Account value      {_usd(report['equity'])}   (started at {_usd(report['start_equity'])})",
        f"  Day trader         {_pct(report['return'])}   as Alpaca recorded it",
        f"  After est. costs   {_pct(report['return_after_costs'])}   (about {COST_BPS:g} bp per side; "
        f"{_usd(report['costs'])} so far)",
        f"  Just holding SPY   {_pct(report['spy_return'])}",
        f"  Difference         {_pct(report['vs_spy_after_costs'])}   after costs",
        f"  Worst dip          {_pct(report['max_drawdown'], signed=False)} below its high",
        f"  Trading days       {report['trading_days']}",
        f"  Trades             {report['trades']}"
        + (f", {report['win_rate']:.0%} winners, average win {_usd(report['avg_win'])}, "
           f"average loss {_usd(report['avg_loss'])}"
           + (f", profit factor {pf:.2f}" if pf else "") + f", {report['stops']} stopped out"
           if report["trades"] and report["win_rate"] is not None else ""),
        "  Holding now        " + (", ".join(report["holding"]) or "nothing (it is flat when not in a trade)"),
    ]
    if report["paused"]:
        lines.append("  PAUSED: no new buys will be made until resumed.")
    lines += ["", report["verdict"]]
    return "\n".join(lines)


def spoken(report: dict) -> str:
    if not report["started"]:
        return "The practice day trader hasn't started yet, so there's nothing to report."
    parts = [f"The practice day-trading account is at {_usd(report['equity'])}, "
             f"{_pct(report['return'])} since it started, or {_pct(report['return_after_costs'])} "
             f"after estimated costs, while just holding SPY would be {_pct(report['spy_return'])}.",
             f"It has made {report['trades']} trade{'s' if report['trades'] != 1 else ''}."]
    if report["paused"]:
        parts.append("Buying is paused.")
    parts.append(report["verdict"])
    if report["source"] == "recorded" and report["as_of"]:
        parts.append("Those are the last recorded numbers.")
    return " ".join(parts)
