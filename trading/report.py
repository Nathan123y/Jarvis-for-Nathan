"""Results, always shown next to what simply holding SPY would have done.

"Is it making money?" is the wrong question on its own: in a rising market
almost anything is. The honest question is whether it beat doing nothing, by
enough, for long enough. This module reports both numbers and says plainly when
the sample is too short to mean anything.
"""
from __future__ import annotations

from typing import Optional

MIN_TRADING_DAYS = 60            # about three months


def _pct(value: Optional[float], signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.1f}%"


def _usd(value: Optional[float]) -> str:
    return "n/a" if value is None else f"${value:,.0f}"


def build(state: dict, snapshots: list[dict], *, account: Optional[dict] = None,
          positions: Optional[dict] = None, spy_price: Optional[float] = None) -> dict:
    """Combine the start line, live (or last-recorded) numbers and the daily snapshots."""
    seen = state.get("last_seen") or {}
    live = account is not None
    equity = account["equity"] if live else seen.get("equity")
    spy_now = spy_price if spy_price else seen.get("spy")
    held = positions if positions is not None else {
        h["symbol"]: {"market_value": h.get("value", 0.0)} for h in seen.get("holdings", [])}
    start_equity, start_spy = state.get("start_equity"), state.get("start_spy")
    started = bool(start_equity and start_spy)

    mine = (equity / start_equity - 1.0) if (started and equity) else None
    spy = (spy_now / start_spy - 1.0) if (started and spy_now) else None
    days = len({s.get("date") for s in snapshots if s.get("date")})

    curve = [start_equity] if started else []
    curve += [s["equity"] for s in snapshots if isinstance(s.get("equity"), (int, float))]
    if equity:
        curve.append(equity)
    peak, worst = 0.0, 0.0
    for value in curve:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)

    total = sum(float(h.get("market_value", 0.0)) for h in held.values())
    rows = sorted(({"symbol": s, "value": float(h.get("market_value", 0.0)),
                    "pct": (float(h.get("market_value", 0.0)) / equity) if equity else 0.0}
                   for s, h in held.items()), key=lambda r: -r["value"])
    report = {
        "started": started, "since": str(state.get("started_at", ""))[:10],
        "source": "live" if live else "recorded", "as_of": seen.get("t", ""),
        "equity": equity, "start_equity": start_equity, "return": mine, "spy_return": spy,
        "vs_spy": (mine - spy) if (mine is not None and spy is not None) else None,
        "trading_days": days, "max_drawdown": worst, "holdings": rows,
        "cash_pct": ((equity - total) / equity) if equity else None,
        "paused": bool(state.get("paused")),
    }
    report["verdict"] = verdict(report)
    return report


def verdict(report: dict) -> str:
    if not report["started"]:
        return "Nothing to judge yet: the first trading day has not happened."
    days, gap = report["trading_days"], report["vs_spy"]
    if days < MIN_TRADING_DAYS:
        return (f"Too early to tell. {days} trading day{'s' if days != 1 else ''} is mostly luck in "
                f"either direction; give it at least {MIN_TRADING_DAYS}, and read the backtest for "
                "the longer view.")
    if gap is None:
        return "Not enough data to compare with SPY yet."
    if gap >= 0:
        return (f"Ahead of simply holding SPY by {gap * 100:.1f} points over {days} trading days. "
                "Still a short sample; check that it also held up in the weak weeks.")
    return (f"Behind simply holding SPY by {-gap * 100:.1f} points over {days} trading days. "
            "A trend rule lags in a steadily rising market and earns its keep in falls.")


def render(report: dict) -> str:
    if not report["started"]:
        return "The paper trader has not started yet. Run:  python3 -m trading run"
    lines = [
        f"Practice account since {report['since']}  ({report['source']} numbers"
        + (f", as of {report['as_of']}" if report["as_of"] else "") + ")",
        f"  Account value   {_usd(report['equity'])}   (started at {_usd(report['start_equity'])})",
        f"  Jarvis          {_pct(report['return'])}",
        f"  Just holding SPY {_pct(report['spy_return'])}",
        f"  Difference      {_pct(report['vs_spy'])}",
        f"  Worst dip       {_pct(report['max_drawdown'], signed=False)} below its high",
        f"  Trading days    {report['trading_days']}",
        "  Holding         " + (", ".join(f"{r['symbol']} {_pct(r['pct'], signed=False)}"
                                          for r in report["holdings"]) or "nothing") +
        f"; cash {_pct(report['cash_pct'], signed=False)}",
    ]
    if report["paused"]:
        lines.append("  PAUSED: no new orders will be sent until resumed.")
    lines += ["", report["verdict"]]
    return "\n".join(lines)


def spoken(report: dict) -> str:
    """Two or three short sentences for Jarvis to say."""
    if not report["started"]:
        return "The practice trader hasn't started yet, so there's nothing to report."
    held = ", ".join(r["symbol"] for r in report["holdings"][:4]) or "cash only"
    parts = [f"The practice account is at {_usd(report['equity'])}, "
             f"{_pct(report['return'])} since it started, while just holding SPY would be "
             f"{_pct(report['spy_return'])}.",
             f"It's holding {held}."]
    if report["paused"]:
        parts.append("Trading is paused.")
    parts.append(report["verdict"])
    if report["source"] == "recorded" and report["as_of"]:
        parts.append("Those are the last recorded numbers.")
    return " ".join(parts)


def latest_per_day(events: list[dict]) -> list[dict]:
    """Keep the last snapshot recorded on each date, oldest first."""
    by_day: dict[str, dict] = {}
    for event in events:
        if event.get("date"):
            by_day[str(event["date"])] = event
    return [by_day[day] for day in sorted(by_day)]
